# Cycle 2 Minutes — Run 17

- **Date:** 2026-05-23
- **Subject:** Reference AgentTeamsWrapper subclass + end-to-end integration test
- **Participants:** Akira Sato (backend-engineer-1), Vivienne Holt (sdet-1, independent reviewer)
- **Status:** success
- **Fix-loop iterations:** 2 (initial passed all invariants but missing team_delete-last assertions in 2 of 4 E2E tests; iter 2 strengthened both)

## Context

Cycle 1 added `tools_provider` plumbing to `orchestrate_run`. This cycle ships the missing pieces:
1. A reference `AgentTeamsWrapper` subclass (`CallbackWrapper`) demonstrating the production wiring pattern (3 callbacks via constructor)
2. An end-to-end integration test driving a real cycle through `orchestrate_run` with `mode='team'` + a real `tools_provider` building `CallbackWrapper` with scripted-mock callbacks

## Decisions

1. **`examples/agent_teams_wrapper_example.py::CallbackWrapper`** — pure-dispatch subclass with 3 keyword-only callable kwargs (`team_create_cb`, `send_message_cb`, `team_delete_cb`). Each validated for callability with `TypeError`. No logic beyond dispatch.
2. **`tests/test_end_to_end_team_mode.py`** drives the REAL `team_cycle_executor` (no monkeypatch on the executor itself). Only stubs the standard infrastructure (`run_ci_checks`, `commit_cycle`, `subprocess.run` for `git rev-parse`).
3. **Shared `op_log`** threaded through all 3 callbacks via `_build_callback_wrapper` helper — every E2E test can assert lifecycle order with one line. Critical for catching the "finally clause removed but team_delete still called on success branch" regression class.
4. **Reference example uses `examples/` package** — `__init__.py` is a deliberate package marker with a docstring explaining "non-production code". No packaging risk (pyproject.toml has no setuptools discovery).
5. **`internal/cycle/SKILL.md`** points at the reference + the E2E test as the integration proof. Production dogfood now only requires the orchestrating Claude session writing 3 real CC-tool callbacks.

## Implementation

- **New:** `examples/__init__.py` + `examples/agent_teams_wrapper_example.py` (~75 LOC)
- **New:** `tests/test_agent_teams_wrapper_example.py` (7 tests)
- **New:** `tests/test_end_to_end_team_mode.py` (6 tests including 2 bonus: type-pin + clone_dir-pass-through)
- **Modified:** `internal/cycle/SKILL.md` — `mode='team'` section now references the example + E2E test

## Fix-loop iteration history

**Iteration 1:** 13 new tests, ruff clean, full pytest 384/385 passes. Implementer flagged scripted-response substring matching as known brittleness (same pattern as cycle 4 of run 15).

**Independent review (Vivienne Holt, SDET):** 0 BLOCKERS. CallbackWrapper signatures match Protocol exactly, E2E tests drive real executor, 3 TypeError validators target correct params, no substring collisions, no regressions. **1 MAJOR**: the load-bearing `team_delete fires LAST` invariant asserted in only 1 of 4 E2E tests; happy-path and abandon-at-agenda tests had the comment but missing the assertion. A future refactor losing the `finally` clause would silently regress on the success path.

**Iteration 2:** refactored `_build_callback_wrapper` to thread a shared `op_log` through all 3 callbacks. Both flagged tests now assert `op_log[-1] == "team_delete"` + bracketing (op_log[0] == "team_create", counts == 1, no send_message after team_delete). Abandon-path test additionally asserts `"send_message" in op_log[:-1]` proving the `finally` actually fires AFTER the PM's ABANDON: response. Unused `unittest.mock.patch` import dropped. Test count unchanged (assertions strengthened, no new tests). Re-review walked through the regression scenario and verified the abandon-path assertion CATCHES the "finally removed" bug class.

**Re-review:** every fix VERIFIED. Cycle-1 + Cycle-2 invariants RE-AFFIRMED. Verdict: **READY TO COMMIT**.

## What this unlocks

**End-to-end production wiring is now provably possible.** All Python-side machinery for `--mode team` is wired and integration-tested. The only remaining step for true team-mode dogfood is the orchestrating Claude session providing 3 real CC-tool callbacks:

```python
from examples.agent_teams_wrapper_example import CallbackWrapper
from scripts.run import orchestrate_run

def make_real_wrapper(clone_dir, project, run_row, cycle_n):
    return CallbackWrapper(
        team_create_cb=lambda name, members: TeamCreate(name=name, members=members),
        send_message_cb=lambda team_id, to, message: SendMessage(team_id=team_id, to=to, message=message),
        team_delete_cb=lambda team_id: TeamDelete(team_id=team_id),
    )

orchestrate_run(..., mode='team', tools_provider=make_real_wrapper)
```

That's a one-time skill/CLAUDE.md doc task. The architectural bridge is complete.

## Residual (minor — deferred)

- The scripted-response substring matching in the test fixture is the same brittleness pattern from `tests/test_team_executor.py` (no new contract invented). If dispatch templates ever change wording significantly, the fixture would need updates. Documented as known limitation in the test module docstring.
- The 2 bonus tests (`test_e2e_team_mode_uses_real_callback_wrapper_not_recording_wrapper`, `test_e2e_team_mode_clone_dir_is_passed_into_provider`) could be merged into the happy-path test for compactness; kept separate per reviewer's "not redundant" assessment.
