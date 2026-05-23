# Cycle 2 Minutes — Run 15

- **Date:** 2026-05-23
- **Subject:** Phase 5b' fix-loop iteration counter helper (PR#22 deferred — converts prose contract into mechanical code)
- **Participants:** Akira Sato (backend-engineer-1, implementer), Sara Lindqvist (prompt-engineer-1, independent reviewer)
- **Status:** success
- **Fix-loop iterations:** 2 (initial impl passed state machine but had 5 majors — silent severity typo, dead code in SKILL, undefined variable, undocumented collision, asserts strippable under `-O`)

## Context

PR#22 added the 4 structured columns (`review_iteration_count`, `unresolved_findings`, `convergence_summary`, `reviewer_attribution`) but no Python helper drove the fix loop or constructed the abandonment outcome. The Phase 5b' invariants (max 5 iterations, exit conditions, abandonment shape) lived only in `internal/cycle/SKILL.md:243-272` prose.

## Decisions

1. **`scripts/fix_loop.py`** is the single source of truth for Phase 5b' fix-loop control flow. Pure-function module with `FixLoopState` dataclass, `Finding` frozen dataclass, `FixLoopExhausted` exception. No I/O, no DB.
2. **Strict severity validation.** `Finding.__post_init__` rejects any severity not in `{blocker, major, minor, nit}` — a typo like `"blocer"` would have silently been treated as non-blocking and hidden real blockers.
3. **Schema-invariant checks use `RuntimeError`, not `assert`.** `build_abandonment_outcome` verifies its output is accepted by cycle 1's allowlist guard (`scripts/run.py`'s `VALID_PHASES`/`VALID_REASONS`) — these checks must hold under `python3 -O`.
4. **SKILL.md wiring is mechanical.** Phase 5b' fix-loop prose now shows the exact helper invocation; iteration cap, convergence gate, and abandonment outcome construction all come from the helper.
5. **Documented collision policy** — `reviewer_attribution` is last-write-wins on duplicate `finding_id` per Python dict semantics; callers should dedupe upstream.

## Implementation

- **New:** `scripts/fix_loop.py` (+185 LOC), `tests/test_fix_loop.py` (+167 LOC, 12 tests)
- **Modified:** `internal/cycle/SKILL.md` Phase 5b' fix-loop section (+~30 LOC: helper invocation block + `pm_ruling_here` explanation)

## Fix-loop iteration history

**Iteration 1:** 11 tests, state machine clean, all probes pass.

**Independent review (Sara Lindqvist):** state machine clean — no blockers. 5 majors:
- Severity validation missing (silent typo class-of-bug)
- SKILL.md example contained dead `if state.history[-1] ... break ... break` with inverted comment
- `pm_ruling_here` referenced but undefined in surrounding prose
- `reviewer_attribution` collision policy undocumented
- Module-level `assert` would be stripped under `python3 -O`

**Iteration 2:** all 5 majors fixed. +1 test for severity validation. Test count 293 → 294.

**Re-review:** every major VERIFIED with file:line proof. `__post_init__` confirmed to fire on frozen-dataclass construction (probed live). Verdict: **READY TO COMMIT**.

## What this unlocks

The Phase 5b' fix loop is now mechanical. When `start_iteration` raises `FixLoopExhausted` after 5 rounds, `build_abandonment_outcome` constructs the exact `review_unrecoverable` dict that cycle 1's `scripts/run.py` allowlist guard accepts — closing the loop from PR#22 (which added the columns) through cycle 1 (which added the orchestrator guard) to this cycle (which adds the producer). All three layers share `scripts/abandonment.VALID_PHASES`/`VALID_REASONS` as the canonical enum source.
