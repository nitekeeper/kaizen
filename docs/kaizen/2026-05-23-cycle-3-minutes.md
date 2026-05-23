# Cycle 3 Minutes — Run 15

- **Date:** 2026-05-23
- **Subject:** scripts/dag.py — Phase 3 Action Items DAG validation gates (3-agent audit item #5, PR#22-era deferred)
- **Participants:** Akira Sato (backend-engineer-1, implementer), Marcus Holbrook (agent-systems-architect-1, independent reviewer)
- **Status:** success
- **Fix-loop iterations:** 2 (initial passed all algorithmic invariants; review found 5 shape-validation + error-noise majors)

## Context

`internal/synthesis-meeting/SKILL.md:133-139` defined 4 DAG validation gates as prose only. A malformed DAG would slip into Phase 4 wave dispatch and break the parallel implementation flow with confusing downstream errors. This cycle ships the helper that converts the prose into mechanical Python.

## Decisions

1. **`scripts/dag.py`** is the single source of truth for Phase 3 DAG validation. 4 gates: acyclic, no within-wave file contention, reads satisfiable, no orphan deps. Pure functions, no I/O.
2. **Strict shape validation.** `_check_item_shape` rejects: missing required keys, non-string `id`, non-string elements in `touches`/`reads`/`depends_on`, intra-item duplicates in any list field. The 4 gate validators assume valid shape — shape bugs are caller bugs surfaced as `ValueError`, not `DAGValidationError`.
3. **Error collection over short-circuit.** `validate_dag` returns a `ValidationResult` containing ALL gate failures so the synthesis meeting can surface every issue in one round rather than play whack-a-mole.
4. **Kahn's algorithm for waves.** `topological_waves` produces a single topological frame even for disconnected components — Wave 1 = all in-degree-zero nodes regardless of connectivity. Cycles raise `CycleDetectedError` naming the cycle members.
5. **`wave` field is informational on input.** `validate_dag` recomputes from `depends_on` (source of truth). The `wave` field IS required (matches the SKILL Action Items schema) but its value is overwritten by the topological computation.
6. **SKILL.md wired.** Synthesis-meeting SKILL Phase 3 now calls the helper BEFORE posting the DAG to the shared task list. Per-gate prose preserved as the contract the helper enforces.

## Implementation

- **New:** `scripts/dag.py` (+248 LOC — 4 error subclasses, `ValidationResult` dataclass, `_check_item_shape`, `topological_waves`, `validate_dag`)
- **New:** `tests/test_dag.py` (+~290 LOC, 18 tests)
- **Modified:** `internal/synthesis-meeting/SKILL.md` (+20 LOC — helper invocation block before per-gate prose)

## Fix-loop iteration history

**Iteration 1:** 13 tests, all algorithmic invariants correct (self-loop, disconnected, multi-error collection, gate-3 produced_so_far walks earlier waves only — all confirmed by reviewer probes).

**Independent review (Marcus Holbrook, agent-systems-architect):** 0 blockers, 5 majors — all about shape validation gaps that would produce confusing downstream errors:
- non-string elements in `id`/`touches`/`reads`/`depends_on` silently misreported (`depends_on=[3]` → OrphanDependencyError masks the type bug)
- intra-item duplicate `touches=["x.py","x.py"]` produced "A touched by A" false-positive contention
- duplicate `depends_on=["MISSING","MISSING"]` fired 2 identical orphan errors
- docstring listed `wave` as required but `_REQUIRED_KEYS` omitted it
- 5 new edge-case tests needed (self-loop, disconnected, non-str id/element, intra-item duplicate)

**Iteration 2:** all 5 majors fixed in one patch. Shape check rejects non-string elements + intra-item duplicates BEFORE gates run. Test count 307 → 312. The intra-item-duplicate test explicitly asserts `not isinstance(excinfo.value, FileContentionError)` — defends against accidental catch since `FileContentionError` is a `ValueError` subclass.

**Re-review:** every major VERIFIED. Reviewer also confirmed adding `wave` to `_REQUIRED_KEYS` doesn't break any existing test (factory fixture always sets it). Verdict: **READY TO COMMIT**.

## What this unlocks

Phase 3 of every kaizen cycle now produces a provably-valid Action Items DAG before posting to the shared task list. When `validate_dag` fails, the synthesis meeting has actionable error messages naming the cycle members / contended file / unsatisfiable read / orphan dependency. No Phase 4 dispatch can start with a malformed DAG. The audit's anti-recommendation ("don't add dag.py first, no meeting agent consumes it yet") is acknowledged — this lands as substrate ready for the meeting agent when it's built.
