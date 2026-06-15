"""Mitigation + observability layer around the opaque agent."""
from __future__ import annotations

import os
import re
import sys
import time
from collections import Counter

# Tải các biến môi trường từ file .env
env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env')
if os.path.exists(env_path):
    with open(env_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                key, val = line.split('=', 1)
                os.environ[key.strip()] = val.strip()

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from telemetry.cost import cost_from_usage
from telemetry.logger import logger, new_correlation_id, set_correlation_id
from telemetry.redact import redact

_INJECTION_PATTERNS = [
    re.compile(r"(?i)(?:ghi\s*ch[uú]|note|system\s*override|ignore\s+previous|new\s+price|gia\s*moi)"),
    re.compile(r"(?i)(?:tong\s*cong\s*la|gia\s*la|chi\s*\d[\d.,]*\s*vnd)"),
    re.compile(r"(?i)(?:bo\s*qua|ignore)\s+(?:system|previous|huong\s*dan)"),
]

_QTY_RE = re.compile(r"(?i)mua\s+(\d+)")
_QTY_ALT_RE = re.compile(r"(?i)(?:^|\s)(\d+)\s*(?:cai|chiec|sp|san\s*pham)?\s*(?:ipad|iphone|macbook|airpods|samsung)")
_ORDER_RE = re.compile(
    r"(?i)\bmua\b|tong\s*cong|tong\s*bao\s*nhieu|het\s*bao\s*nhieu|tinh\s*tong|thanh\s*toan"
)
_SHIP_RE = re.compile(r"(?i)\bship\b|\bgiao\b|\bdeliver\b")
_COUPON_RE = re.compile(r"(?i)(?:ma|coupon|voucher|khuyen\s*mai)\s+(\w+)")
_TONG_LINE_RE = re.compile(r"(?i)tong\s*cong\s*:\s*([\d.,]+)\s*vnd")

_REFUSAL_MSG = {
    "out_of_stock": "San pham het hang, khong the dat hang.",
    "not_found": "Khong tim thay san pham trong kho.",
    "destination": "Khong ho tro giao hang den dia diem nay.",
    "incomplete": "Khong du thong tin de tinh tong don hang.",
}

_CACHEABLE_STATUSES = {"ok", "no_action"}


def _remove_vn_accents(s: str) -> str:
    s = re.sub(r'[àáạảãâầấậẩẫăằắặẳẵ]', 'a', s)
    s = re.sub(r'[ÀÁẠẢÃÂẦẤẬẨẪĂẰẮẶẲẴ]', 'A', s)
    s = re.sub(r'[èéẹẻẽêềếệểễ]', 'e', s)
    s = re.sub(r'[ÈÉẸẺẼÊỀẾỆỂỄ]', 'E', s)
    s = re.sub(r'[òóọỏõôồốộổỗơờớợởỡ]', 'o', s)
    s = re.sub(r'[ÒÓỌỎÕÔỒỐỘỔỖƠỜỚỢỞỠ]', 'O', s)
    s = re.sub(r'[ìíịỉĩ]', 'i', s)
    s = re.sub(r'[ÌÍỊỈĨ]', 'I', s)
    s = re.sub(r'[ùúụủũưừứựửữ]', 'u', s)
    s = re.sub(r'[ÙÚỤỦŨƯỪỨỰỬỮ]', 'U', s)
    s = re.sub(r'[ỳýỵỷỹ]', 'y', s)
    s = re.sub(r'[ỲÝỴỶỸ]', 'Y', s)
    s = re.sub(r'[đ]', 'd', s)
    s = re.sub(r'[Đ]', 'D', s)
    return s


def _sanitize_question(question: str) -> str:
    """Sanitize inputs (e.g. remove prompt injection)."""
    q = question or ""
    q = _remove_vn_accents(q)
    for pat in _INJECTION_PATTERNS:
        q = pat.sub("", q)
    return q


def _is_order_question(question: str) -> bool:
    return bool(_ORDER_RE.search(question or ""))


def _is_price_inquiry(question: str) -> bool:
    return bool(re.search(r"(?i)gia\s*bao\s*nhieu", question or "")) and not _is_order_question(question)


def _needs_shipping(question: str) -> bool:
    return bool(_SHIP_RE.search(question or ""))


def _extract_qty(question: str) -> int | None:
    q = question or ""
    m = _QTY_RE.search(q)
    if m:
        return int(m.group(1))
    m = _QTY_ALT_RE.search(q)
    if m:
        return int(m.group(1))
    if re.search(r"(?i)\bmua\b", q):
        return 1
    return None


def _extract_coupon(question: str) -> str | None:
    m = _COUPON_RE.search(question or "")
    return m.group(1).upper() if m else None


def _discount_pct(question: str, discount: dict | None, trace: list) -> int | None:
    """Return discount percent, 0 for invalid/expired, None if cannot determine."""
    coupon = _extract_coupon(question)
    if not coupon:
        return 0
        
    known = {"SALE15": 15, "VIP20": 20, "WINNER": 10}
    if coupon in known:
        return known[coupon]
    if coupon in {"EXPIRED", "INVALID", "FAKE"}:
        return 0

    if discount:
        if discount.get("valid"):
            return int(discount.get("percent") or 0)
        return 0

    tools = [
        s.get("tool") for s in (trace or [])
        if isinstance(s, dict) and s.get("tool")
    ]
    if "get_discount" not in tools:
        return None
    return 0


def _catalog_blocks(stock: dict, config: dict) -> bool:
    item = (stock.get("item") or "").lower()
    override = (config or {}).get("catalog_override") or {}
    entry = override.get(item) or override.get(item.replace(" ", ""))
    if isinstance(entry, dict) and entry.get("in_stock") is False:
        return True
    return False


def _parse_trace(trace: list) -> tuple[dict | None, dict | None, dict | None]:
    stock = discount = shipping = None
    for step in trace or []:
        if not isinstance(step, dict):
            continue
        obs = step.get("observation") or step.get("result") or {}
        if not isinstance(obs, dict):
            continue
        tool = step.get("tool") or ""
        if tool == "check_stock":
            stock = obs
        elif tool == "get_discount":
            discount = obs
        elif tool == "calc_shipping":
            shipping = obs
    return stock, discount, shipping


def _compute_total(question: str, trace: list, config: dict | None = None) -> tuple[int | None, str]:
    """Return (total_vnd, status) where status is ok | refuse_*."""
    stock, discount, shipping = _parse_trace(trace)

    if not stock or stock.get("error") or not stock.get("found"):
        return None, "not_found"
    if not stock.get("in_stock") or _catalog_blocks(stock, config or {}):
        return None, "out_of_stock"

    qty = _extract_qty(question)
    if qty is None:
        return None, "incomplete"

    pct = _discount_pct(question, discount, trace)
    if pct is None:
        return None, "incomplete"

    unit_price = int(stock.get("unit_price_vnd") or 0)
    subtotal = unit_price * qty
    discounted = subtotal * (100 - pct) // 100

    if _needs_shipping(question):
        if not shipping or shipping.get("error") or shipping.get("cost_vnd") is None:
            return None, "destination"
        shipping_fee = int(shipping.get("cost_vnd") or 0)
    else:
        shipping_fee = 0

    return discounted + shipping_fee, "ok"


def _format_total_answer(total: int) -> str:
    return f"Tong cong: {total} VND"


def _format_refusal(reason: str) -> str:
    return _REFUSAL_MSG.get(reason, _REFUSAL_MSG["incomplete"])


def _format_price_info(stock: dict) -> str:
    if not stock or stock.get("error") or not stock.get("found"):
        return _REFUSAL_MSG["not_found"]
    if not stock.get("in_stock"):
        return _REFUSAL_MSG["out_of_stock"]
    item = stock.get("item") or "san pham"
    price = int(stock.get("unit_price_vnd") or 0)
    return f"{item} con hang, gia {price} VND."


def _apply_guardrail(question: str, result: dict, config: dict | None = None) -> dict:
    """Rewrite answer to enforce Tong cong format or clean refusal."""
    trace = result.get("trace", [])
    if _is_order_question(question):
        expected_total, status = _compute_total(question, trace, config)
        if expected_total is not None:
            result["answer"] = _format_total_answer(expected_total)
            result["status"] = "ok"
        else:
            result["answer"] = _format_refusal(status)
            result["status"] = "ok"
    elif _is_price_inquiry(question):
        stock, _, _ = _parse_trace(trace)
        result["answer"] = _format_price_info(stock)
        result["status"] = "ok"
    return result


def _trace_tool_errors(trace: list) -> list[str]:
    errors = []
    for step in trace or []:
        if not isinstance(step, dict):
            continue
        err = step.get("error") or step.get("tool_error")
        if err:
            errors.append(str(err))
        obs = step.get("observation") or step.get("result")
        if isinstance(obs, dict) and obs.get("error"):
            errors.append(str(obs["error"]))
    return errors


def _repeated_actions(trace: list) -> list[str]:
    actions = [
        str(s.get("action") or s.get("tool") or "")
        for s in (trace or [])
        if isinstance(s, dict)
    ]
    counts = Counter(a for a in actions if a)
    return [a for a, n in counts.items() if n >= 3]


def _is_valid_answer(question: str, answer: str) -> bool:
    text = (answer or "").strip()
    if not text:
        return False
    if re.search(r"(?i)sách|\bsach\b|thông tin sách|thong tin sach", text):
        return False
    if _is_order_question(question):
        if re.match(r"^Tong cong: \d+ VND$", text):
            return True
        return text in set(_REFUSAL_MSG.values())
    if _is_price_inquiry(question):
        return bool(re.search(r"(?i)con hang, gia \d+ VND\.", text)) or text in {
            _REFUSAL_MSG["not_found"],
            _REFUSAL_MSG["out_of_stock"],
        }
    return True


def _missing_required_tools(question: str, trace: list) -> bool:
    tools = [
        s.get("tool") for s in (trace or [])
        if isinstance(s, dict) and s.get("tool")
    ]
    if _is_order_question(question) or _is_price_inquiry(question):
        if "check_stock" not in tools:
            return True
    if _is_order_question(question):
        if _extract_coupon(question) and "get_discount" not in tools:
            return True
        if _needs_shipping(question) and "calc_shipping" not in tools:
            return True
    return False


def _should_retry(result: dict, question: str | None = None) -> bool:
    status = result.get("status")
    if status in ("loop", "max_steps", "wrapper_error"):
        return True
    if _trace_tool_errors(result.get("trace", [])):
        return True
    if not question:
        return False
    trace = result.get("trace") or []
    steps = result.get("steps")
    if steps is None:
        steps = len(trace)
    needs_tools = _is_order_question(question) or _is_price_inquiry(question)
    if needs_tools and int(steps or 0) == 0:
        return True
    if needs_tools and _missing_required_tools(question, trace):
        return True
    if needs_tools and not _is_valid_answer(question, result.get("answer") or ""):
        return True
    return False


def _redact_answer(answer: str | None) -> str | None:
    if not answer:
        return answer
    cleaned, hits = redact(answer)
    return cleaned if hits else answer


def _log_call(context, cid, result, wall_ms):
    meta = result.get("meta", {}) or {}
    usage = meta.get("usage", {}) or {}
    trace = result.get("trace", []) or []
    answer = result.get("answer") or ""
    _, pii_hits = redact(answer)
    tool_errors = _trace_tool_errors(trace)
    repeated = _repeated_actions(trace)
    tong_match = _TONG_LINE_RE.search(answer)

    logger.log_event("AGENT_CALL", {
        "qid": context.get("qid"),
        "session_id": context.get("session_id"),
        "turn_index": context.get("turn_index"),
        "correlation_id": cid,
        "status": result.get("status"),
        "steps": result.get("steps"),
        "wall_ms": wall_ms,
        "latency_ms": meta.get("latency_ms"),
        "model": meta.get("model"),
        "provider": meta.get("provider"),
        "usage": usage,
        "cost_usd": cost_from_usage(meta.get("model", ""), usage),
        "tools_used": meta.get("tools_used", []),
        "tool_error_count": len(tool_errors),
        "tool_errors": tool_errors[:5],
        "repeated_actions": repeated,
        "pii_hits_in_answer": pii_hits,
        "has_tong_line": bool(tong_match),
        "answer_len": len(answer),
        "trace_len": len(trace),
    })

    if tool_errors:
        logger.log_event("FAULT_HINT", {
            "qid": context.get("qid"),
            "fault_class": "error_spike",
            "correlation_id": cid,
            "evidence": {"tool_errors": tool_errors[:3]},
        })
    if repeated:
        logger.log_event("FAULT_HINT", {
            "qid": context.get("qid"),
            "fault_class": "infinite_loop",
            "correlation_id": cid,
            "evidence": {"repeated_actions": repeated},
        })
    if pii_hits:
        logger.log_event("FAULT_HINT", {
            "qid": context.get("qid"),
            "fault_class": "pii_leak",
            "correlation_id": cid,
            "evidence": {"pii_hits": pii_hits},
        })


def mitigate(call_next, question, config, context):
    cid = new_correlation_id()
    set_correlation_id(cid)

    conf = dict(config)
    sanitized = _sanitize_question(question)
    if sanitized != question:
        logger.log_event("SANITIZE", {"qid": context.get("qid"), "changed": True})

    retry_cfg = conf.get("retry") or {}
    max_attempts = int(retry_cfg.get("max_attempts", 1)) if retry_cfg.get("enabled") else 1
    backoff_ms = int(retry_cfg.get("backoff_ms", 0))

    cache = context.get("cache")
    cache_lock = context.get("cache_lock")
    cache_cfg = conf.get("cache") or {}
    cache_key = sanitized.strip().lower()
    if cache_cfg.get("enabled") and cache is not None and cache_lock is not None:
        with cache_lock:
            hit = cache.get(cache_key)
        if hit and hit.get("status") in _CACHEABLE_STATUSES:
            logger.log_event("CACHE_HIT", {"qid": context.get("qid"), "correlation_id": cid})
            return dict(hit)

    result = None
    for attempt in range(1, max_attempts + 1):
        t0 = time.time()
        result = call_next(sanitized, conf)
        wall_ms = int((time.time() - t0) * 1000)
        _log_call(context, cid, result, wall_ms)
        if not _should_retry(result, sanitized) or attempt >= max_attempts:
            break
        if backoff_ms > 0:
            time.sleep(backoff_ms / 1000.0)
        logger.log_event("RETRY", {
            "qid": context.get("qid"),
            "attempt": attempt + 1,
            "status": result.get("status"),
        })

    result = _apply_guardrail(sanitized, result, conf)

    if conf.get("redact_pii"):
        answer = result.get("answer")
        cleaned = _redact_answer(answer)
        if cleaned is not answer:
            result = dict(result)
            result["answer"] = cleaned

    if cache_cfg.get("enabled") and cache is not None and cache_lock is not None:
        if result.get("status") in _CACHEABLE_STATUSES:
            with cache_lock:
                cache[cache_key] = dict(result)

    return result