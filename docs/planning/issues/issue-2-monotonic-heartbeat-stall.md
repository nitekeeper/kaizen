---
title: "[medium] Monotonic-friendly heartbeat stall check (laptop-suspend skew)"
labels: bug
---

## Context

`_s1_seconds_since_last_poll()` uses SQLite `julianday('now')` minus `bridge_heartbeat.last_polled_at` to detect S1 stalls. On laptop suspend/resume, the wall clock can jump 8h forward while `time.monotonic()` stays still — a session resumed into a stale-by-wall-clock state would trip `BridgeStallError` and abandon the cycle. Current `HEARTBEAT_STALL_S=300` mitigates short suspends but not overnight closes. Kaizen's primary deployment surface is a laptop the user closes between sessions, so this failure mode is realistic and recurring (classified Medium, not Low).

## Where

- `scripts/cc_tool_bridge.py:407` — TODO comment in `_s1_seconds_since_last_poll()` docstring
  (line drifted from `:194` in original memory note; verified against `main @ 3a1251b`)

## Suggested approach

- Hybrid check: raise `BridgeStallError` ONLY when both (`julianday('now')` gap > `HEARTBEAT_STALL_S`) AND (monotonic gap since last `_tick_python_heartbeat()` > `HEARTBEAT_STALL_S`)
- If julianday gap is large but monotonic gap is small, treat as suspect (likely suspend-resume), reset the deadline, and continue
- Alternative: detect suspend via `psutil.boot_time()` or `/proc/uptime` jumps and reset the deadline on detected resume
- Add a regression test that simulates a wall-clock jump without a monotonic-clock jump and asserts no spurious stall

## Acceptance criteria

- [ ] Stall check survives a simulated wall-clock jump > `HEARTBEAT_STALL_S` when monotonic clock has not advanced (no `BridgeStallError`)
- [ ] Stall check still fires when both clocks agree S1 has been silent past threshold
- [ ] Includes a regression test simulating a wall-clock jump > `HEARTBEAT_STALL_S` while monotonic clock stays still; asserts no spurious `BridgeStallError`
- [ ] Test in `tests/test_cc_tool_bridge.py` also covers the "both clocks agree, do raise" case
- [ ] TODO comment at `scripts/cc_tool_bridge.py:407` removed

## Related

- Origin: PR review round 1 (marker `m4` DEFER)
- If implemented alongside #42 (per-cycle outer deadline), prefer landing in a single PR to avoid two rounds of churn in `scripts/cc_tool_bridge.py`
- Context doc: `docs/planning/deferred-todos.md` item 2
