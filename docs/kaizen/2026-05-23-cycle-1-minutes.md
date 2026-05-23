# Cycle 1 Minutes — Run 17

- **Date:** 2026-05-23
- **Subject:** orchestrate_run tools_provider plumbing — bridge the team-mode integration gap
- **Participants:** Akira Sato (backend-engineer-1), Diane Park (sdet-1, independent reviewer)
- **Status:** success
- **Fix-loop iterations:** 1 (reviewer found 0 blockers + 0 majors on first pass)

## Context

After PR#24, team-mode had all the substrate (lifecycle skeleton, real Phase 1-5c orchestration, dispatch templates, AgentTeamsWrapper base class) but `scripts/run.py::orchestrate_run` had no way to thread a `TeamTools` wrapper into `team_cycle_executor`. Every `--mode team` invocation crashed at the `tools is None` preflight, AFTER `create_run` + clone + seed + branch had already executed (garbage on disk + an orphan run row).

## Decisions

1. **`orchestrate_run` gains a `tools_provider` kwarg** — `Callable[[Path, dict, dict, int], TeamTools] | None`. When `mode='team'` and provider is set, invoked once per cycle and the result is threaded into `cycle_executor` via the existing `tools=` kwarg.
2. **Fail-fast guard BEFORE any side effect** — when `mode='team'` AND `tools_provider is None` AND no explicit `cycle_executor` was injected: raise `ValueError` after project resolution but BEFORE clone/seed/branch/run-row. No garbage left on disk or in the DB. Error message names both `'tools_provider'` and `'mode=team'` so the fix is obvious.
3. **Subagent mode unchanged** — `tools_provider` is ignored when `mode='subagent'`. All 365 prior tests pass unchanged.
4. **Defense-in-depth preserved** — `team_cycle_executor`'s own `tools is None → TeamToolsUnavailableError` guard stays AS-IS. Anyone calling the executor directly (bypassing `orchestrate_run`) still gets a clear error.
5. **Guard skipped when explicit cycle_executor injected** — principled deviation: tests injecting their own executor are bypassing `team_cycle_executor` entirely, so the wrapper is irrelevant. Reviewer verified by grep that no production caller hits this path; CLI `main()` never passes `cycle_executor`.

## Implementation

- **Modified:** `scripts/run.py` (+44/-3) — new `tools_provider` kwarg, fail-fast guard, per-cycle dispatch branch
- **Modified:** `tests/test_run.py` (+320/-30) — 6 new tests + 1 rewritten test (`test_orchestrate_run_selects_team_executor_when_mode_team` now pins the fail-fast contract instead of the old H3 "row marked failed" behavior)
- **Modified:** `internal/run/SKILL.md` — Step 6 callout for team-mode wrapper requirement
- **Modified:** `skills/improve/SKILL.md` — `--mode team` wrapper-construction note
- **Unchanged:** `scripts/team_executor.py` (defense-in-depth preserved)

## Fix-loop iteration history

**Iteration 1:** 6 new tests + 1 rewritten + 4-file diff, all 7 critical invariants intact.

**Independent review (Diane Park, SDET):** verified all invariants. **0 BLOCKERS, 0 MAJORS, 3 minors** — all optional polish (keyword-only enforcement, type-hint addition, defense-in-depth mention in error message). Verdict: **READY TO COMMIT** on first pass — no fix iteration needed.

## What this unlocks

The Python ↔ orchestrating-agent integration is now wired. Cycle 2 will ship a reference `AgentTeamsWrapper` subclass + end-to-end integration test that drives a full cycle through `orchestrate_run` with `mode='team'` + `tools_provider`. After cycle 2 lands, the only remaining step for true team-mode dogfood is the orchestrating Claude session writing a one-time `AgentTeamsWrapper` subclass that wraps its own `TeamCreate`/`SendMessage`/`TeamDelete` tool calls.

## Residual (minor — deferred per reviewer)

- `tools_provider` is `POSITIONAL_OR_KEYWORD` (not keyword-only via `*,`). All real callers use kwargs; tightening would be a deliberate API-breaking decision.
- Type hint on `tools_provider` could be added (`Callable[[Path, dict, dict, int], TeamTools] | None`) but the surrounding signature has no hints either.
- ValueError message doesn't mention the defense-in-depth guard in `team_cycle_executor`. Not needed for the caller to fix.
