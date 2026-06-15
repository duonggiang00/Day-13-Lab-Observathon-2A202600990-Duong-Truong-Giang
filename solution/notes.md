# Diagnosis scratchpad

Run the practice simulator, read YOUR telemetry, and note what you find.
Fault classes to hunt: error_spike · latency_spike · cost_blowup · quality_drift ·
infinite_loop · tool_failure · pii_leak.

| symptom (from telemetry) | which requests | suspected cause | config fix? | wrapper fix? |
|---|---|---|---|---|
| Some tool calls fail intermittently | prv-001, prv-010 | API unreliability | Yes (retry) | No |
| Long-tail slow requests | All requests | Repeated processing | Yes (cache) | Yes (thread-safe cache) |
| Tokens far above the task requirement | All requests | self_consistency > 1 | Yes (self_consistency=1) | No |
| Answers worsen in later turns | prv-003, prv-004 | Tool corruption (Drift) | No | Yes (hardcode invariants in wrapper/prompt) |
| Agent repeatedly calls same tools | prv-002, prv-005 | Missing extraction rules | Yes (max_steps=4) | No (fixed in prompt) |
| Diacritic city always fails | prv-005, prv-007 | Unicode mismatch | Yes (normalize_unicode) | Yes (_remove_vn_accents) |
| Agent repeats raw email/phone | prv-020, prv-054 | Prompt implicitly repeats | No | No (fixed in prompt) |
