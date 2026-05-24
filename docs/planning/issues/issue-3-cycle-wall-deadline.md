---
title: "[low] Per-cycle outer wall-clock deadline (CYCLE_WALL_S)"
labels: enhancement
---

## Context

Worst-case bridge wall-clock is `PER_CALL_TIMEOUT_S * dispatches_per_cycle`. A 50-call cycle could in principle block 8h+ before any single-call timeout fires. A per-cycle `CYCLE_WALL_S` (~3600s) cap would bound this. Empirically the per-call timeout has always fired first, but the hole exists.

## Where

- `scripts/cc_tool_bridge.py:438` — TODO comment in `_request()` body
  (line drifted from `:240` in original memory note; verified against `main @ 3a1251b`)

## Suggested approach

- Add `CYCLE_WALL_S = 3600` module constant near `PER_CALL_TIMEOUT_S`
- Set `_cycle_deadline = time.monotonic() + CYCLE_WALL_S` on the first `_request()` call per cycle
- Raise `BridgeStallError("cycle wall-clock exceeded")` when exceeded; include elapsed-seconds context in the message
- Reset `_cycle_deadline` in `finalize_run()` so the next cycle starts fresh
- Unit test: simulate slow polls that aggregate past `CYCLE_WALL_S`; assert the error fires

## Acceptance criteria

- [ ] `CYCLE_WALL_S` module constant added with documented rationale
- [ ] Per-cycle deadline tracked across `_request()` calls and reset by `finalize_run()`
- [ ] Test in `tests/test_cc_tool_bridge.py` covers the aggregate-timeout case
- [ ] TODO comment at `scripts/cc_tool_bridge.py:438` removed

## Related

- Origin: PR#34 review round 1 (architect MINOR finding)
- If implemented alongside #41 (monotonic-friendly stall check), prefer landing in a single PR to avoid two rounds of churn in `scripts/cc_tool_bridge.py`
- Context doc: `docs/planning/deferred-todos.md` item 3
