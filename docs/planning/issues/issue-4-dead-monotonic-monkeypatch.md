---
title: "[low] Remove dead time.monotonic monkeypatch in bridge test"
labels: enhancement
---

## Context

A test in `tests/test_cc_tool_bridge.py` monkeypatches `bridge_mod.time.monotonic`, but the stall predicate being asserted reads SQLite `julianday('now')` — not Python's monotonic clock. The patch fast-forwards the per-call deadline counter (harmless side effect) but is not load-bearing for the assertion. Cosmetic; misleading to future readers.

## Where

- `tests/test_cc_tool_bridge.py:262` — explanatory TODO comment block start
- `tests/test_cc_tool_bridge.py:270` — the `monkeypatch.setattr` call itself
  (original memory note said `:264`; verified against `main @ 3a1251b`)

## Suggested approach

Pick one:
- **(preferred)** Option (a) — delete `fake_monotonic`, `monkeypatch.setattr`, and the TODO comment block. The test still passes today without them and is clearer.
- Fallback — Option (b) — keep the patch and rewrite the comment to plainly state: "Kept to compress the per-call deadline counter so PER_CALL_TIMEOUT_S=600 isn't actually waited out; the stall assertion reads SQLite, not this clock."

## Acceptance criteria

- [ ] Preferred option: (a) — delete `fake_monotonic`, `monkeypatch.setattr`, and the TODO comment block. Test still passes (543 tests green at HEAD `3a1251b`; re-verify at fix time and confirm count does not regress below 543 without a deliberate cause).
- [ ] Fallback option: (b) — patch kept, comment rewritten as above. Acceptable only if (a) causes a regression at fix time.
- [ ] No `TODO(cosmetic)` marker remains at the cited location

## Related

- Context doc: `docs/planning/deferred-todos.md` item 4
