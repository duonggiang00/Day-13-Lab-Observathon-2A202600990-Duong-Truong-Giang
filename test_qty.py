import json
import re

_QTY_RE = re.compile(r"(?i)mua\s+(\d+)")
_QTY_ALT_RE = re.compile(r"(?i)(?:^|\s)(\d+)\s*(?:cai|chiec|sp|san\s*pham)?\s*(?:ipad|iphone|macbook|airpods|samsung)")

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

with open('run_output.json', 'r', encoding='utf-8') as f:
    d = json.load(f)

for r in d['results']:
    question = r['question']
    qty = _extract_qty(question)
    if "mua" not in question.lower() and "gia bao nhieu" not in question.lower():
        print(f"Strange question: {question}")
    if qty == 1 and not re.search(r"(?i)mua\s+1", question) and not re.search(r"(?i)\b1\s*(?:cai|chiec)", question):
        print(f"Fallback 1 qty used for: {question}")
