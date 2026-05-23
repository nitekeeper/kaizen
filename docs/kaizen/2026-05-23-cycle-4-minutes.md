# Cycle 4 Minutes — Run 15

- **Date:** 2026-05-23
- **Subject:** team_executor real implementation (3-agent audit item #4 — flip the NotImplementedError stub)
- **Participants:** Akira Sato (backend-engineer-1, implementer), Diane Park (sdet-1, independent reviewer; lifecycle invariants + dependency injection)
- **Status:** success (skeleton)
- **Fix-loop iterations:** 2 (initial impl locked the lifecycle; review caught 3 robustness gaps; all fixed)

## Context — the user's deliberate override

The 3-agent audit on 2026-05-23 explicitly anti-recommended this cycle: "first team-mode dogfood will crash because team_executor is a stub." The user picked "All 7 anyway" in scope selection, accepting that risk. This cycle ships the substrate to make team-mode dogfood viable, NOT the full Phase 1-5c orchestration.

## Architectural honesty

Python cannot directly invoke Claude Code session tools (`TeamCreate`, `SendMessage`, `TeamDelete`). The fix is a coordinator that accepts injected tool callables (`TeamTools` Protocol). Production callers — the orchestrating agent running `internal/cycle/SKILL.md` with `mode='team'` — provide wrappers from their own tool context. Tests inject mocks. This separates three concerns:

1. **Lifecycle determinism** (Python owns) — team_create → ... → team_delete in `finally`; outcome dict exact shape; allowlist-valid abandonment fields. These are the things you cannot trust LLM-driven flow to get right.
2. **Tool invocation** (orchestrating agent owns) — wraps the actual CC tool calls.
3. **Phase 1-5c semantics** (deferred) — the skeleton sends one message to the first roster member; real agenda/wave/review-loop logic is a future cycle.

## Decisions

1. **`scripts/team_executor.py::team_cycle_executor`** is the coordinator. `tools` parameter is keyword-only; defaults to `None`; raises `TeamToolsUnavailableError` when absent. Production must inject; tests must mock.
2. **`TeamTools` Protocol** + **runtime preflight** — Python `Protocol` is static-only, so the executor performs a `callable(getattr(tools, m, None))` check for every required method before calling team_create. Catches wrappers missing a method without leaking a team.
3. **`team_delete` in `finally`** — fires on every exit path (happy, abandon, send raises). Test_team_delete_fires_even_when_send_message_raises and test_team_delete_fires_when_response_signals_abandon prove it. team_create raise → no team to delete, finally correctly skipped because team_id never bound.
4. **Outcome dict exact shape** — success: 5 keys (`status, subject, commit_sha, minutes_memex_slug, participants`); abandoned: 11 keys including the 4 Phase 5b' optional fields (defaulting to `None` for non-review abandonments). Tests assert via `set(outcome.keys()) == {...}` (exact match, not membership).
5. **Abandon path uses cycle 1's frozensets** — `phase_reached="meeting"` and `reason="other"` are both in `VALID_PHASES`/`VALID_REASONS`. Runtime assertions in the executor + end-to-end test that mirrors `scripts/run.py::orchestrate_run`'s allowlist guard.
6. **Skeleton honesty** — module-top docstring opens with "**Cycle 4 ships only the lifecycle skeleton.**" + inline `# SKELETON` comments + `commit_sha="(skeleton)"` literal + `scripts/pr.py` special-cases this sentinel in PR body rendering. A code reader, a future maintainer, AND the PR-body reader all see the skeleton-ness without git-blame archaeology.

## Implementation

- **Replaced:** `scripts/team_executor.py` — stub `NotImplementedError` → real coordinator with `TeamTools` Protocol, `TeamCycleOutcome` dataclass, lifecycle in `try/finally`, runtime Protocol preflight, allowlist-validated abandonment.
- **Modified:** `scripts/pr.py:158-166` — special-case `commit_sha == "(skeleton)"` so PR body shows `"(skeleton — no real commit, see cycle minutes)"` instead of the truncated `"(skelet"`.
- **Replaced:** `tests/test_team_executor.py` — dropped `test_raises_not_implemented_when_env_set` (codified the old contract); added 12 new tests covering lifecycle, outcome shape, Protocol enforcement, end-to-end allowlist.
- **Modified:** `tests/test_pr.py` — `test_render_pr_body_special_cases_skeleton_commit_sha` regression-guards the sentinel rendering.

## Fix-loop iteration history

**Iteration 1:** 11 new tests, all lifecycle invariants locked down, ruff clean.

**Independent review (Diane Park, SDET — dependency-injection + lifecycle specialist):** 0 BLOCKERS — architecture sound, team_delete-in-finally proven on every exit path, abandoned outcome passes the allowlist guard end-to-end. 3 majors (all robustness polish, not correctness):
- Protocol not runtime-enforced (passing `object()` blows up with confusing AttributeError instead of TeamToolsUnavailableError)
- Abandon-outcome test used `key in outcome` (membership), not `set(keys) == {...}` (exact-match)
- `commit_sha="(skeleton)"` would render as `"(skelet"` in PR body (cosmetic but visible)

**Iteration 2:** all 3 majors fixed in one patch. +2 tests (runtime Protocol enforcement + (skeleton) sentinel PR rendering). Test count 321 → 323.

**Re-review:** every major VERIFIED. The runtime check runs BEFORE team_create. The exact-set assertion matches the production dict literal. The pr.py special case preserves legacy behavior for normal SHAs (verified by all 27 existing pr.py tests still passing). Verdict: **READY TO COMMIT**.

## What this unlocks AND explicitly defers

**Unlocks:** the next kaizen run can attempt `--mode team` against the substrate without the executor immediately crashing — the lifecycle skeleton runs end-to-end with mocked tools, and the lifecycle invariants (team_delete-in-finally, allowlist-valid abandonment, exact outcome shape) are CI-locked.

**Explicitly deferred** (separate future cycle warranted):
1. **Real `TeamTools` wrapper in `internal/cycle/SKILL.md` mode='team' branch** — the orchestrating agent needs to provide the actual CC-tool wrappers. The Protocol is defined; nobody implements it for production yet.
2. **Real Phase 1-5c orchestration** — the current single-`send_message` skeleton stands in for the full agenda/wave/review-loop dispatch.

The skeleton is honest about being a skeleton (multiple visible markers). The next cycle's job is bounded: implement the production wrapper + Phase 1-5c semantics inside the existing `try`-block. The dispatch architecture itself is locked.
