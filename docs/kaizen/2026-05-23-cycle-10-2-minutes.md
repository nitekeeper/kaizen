# Cycle 10-2 Minutes — Phase 4 Redesign (Wave-Based Dispatch)

**Date:** 2026-05-23
**Branch:** kaizen/phase-redesign-2026-05-23-0431
**Cycle:** Run 10, Cycle 2
**Participants:** Dr. Nadia Petrov (Architect), Dr. Samuel Okafor (Backend)
**Subject:** Rewrite Phase 4 of `internal/cycle/SKILL.md` to describe wave-based parallel dispatch via Agent Teams shared task list with dependencies.

---

## Agenda

Implement Phase 4 of the seed design (`docs/design/kaizen-phase-redesign-design.md`) — replacing the single-implementer sequential Phase 4 with a DAG-driven, wave-based, parallel-dispatch model backed by the Agent Teams shared task list.

---

## Discussion

### Architect (Dr. Nadia Petrov)

The existing Phase 4 collapses multi-agent parallelism into a single sequential implementer. The seed design's DAG-with-waves approach is the correct structural remedy. Key architectural decisions:

1. The handoff from Phase 3 is the Action Items DAG (with `depends_on` and `wave` fields already set by the synthesis meeting). Phase 4 does not re-derive the DAG — it consumes it.
2. Execution is graph-driven, not loop-driven. The lead posts tasks to the shared task list; the dependency graph determines unlock order. This removes the orchestrator from the hot path.
3. Wave boundaries are the natural test checkpoint. Running CI after every wave gives fast failure feedback scoped to the changes just landed, without forcing a full-cycle test abort for a single wave's regression.

### Backend (Dr. Samuel Okafor)

Agreed on the graph-driven model. Implementation refinements:

1. File-locked claim semantics are critical for correctness — without them, two teammates could claim the same task and produce conflicting writes. The prose must call this out explicitly (it does, by referencing Agent Teams docs).
2. The in-cycle fix iteration at wave boundaries must be bounded (max 3 rounds) to prevent infinite loops, and the failure escalation path (`tests_unrecoverable`) must be explicit — this mirrors the existing Phase 5b fix-iteration contract.
3. The `SendMessage`-based conflict resolution (replacing the old "mini-synthesis (one item)" escalation) is lighter-weight and keeps the cycle moving; only escalate to lead when 2 exchanges between teammates don't resolve it.

### Joint decisions

| # | Decision |
|---|---|
| D1 | Phase 4 rewritten to wave-based parallel dispatch via Agent Teams shared task list with `depends_on` semantics. |
| D2 | Tests run at wave boundaries using `run_ci_checks` — same call signature as Phase 5b for consistency. |
| D3 | In-cycle fix iteration at wave boundaries: max 3 rounds, abandon as `tests_unrecoverable` if not recovered. Scoped to the failing wave's owners + test-focused experts. |
| D4 | Owner from Phase 3 carries forward as the implementer in Phase 4 — skin in the game across phases. |
| D5 | Mid-implementation conflicts resolved via `SendMessage` between teammates; lead intervenes only after 2 unresolved exchanges. |
| D6 | Failure modes section added: stalled tasks (owner abandons back to pending + failure note) and DAG deadlock (PM ruling or `tests_unrecoverable` abandon). |
| D7 | Before/after comparison table added for clarity on what changed from the pre-redesign Phase 4. |

---

## Action Items

| # | Action | Touches | Owner | Status |
|---|---|---|---|---|
| 1 | Rewrite Phase 4 of `internal/cycle/SKILL.md` | `internal/cycle/SKILL.md` | Dr. Nadia Petrov | ✓ Done |
| 2 | Write cycle minutes | `docs/kaizen/2026-05-23-cycle-10-2-minutes.md` | Dr. Samuel Okafor | ✓ Done |

---

## CI Results

- **pytest:** all tests passed
- **ruff check:** passed (no lint errors)
- **ruff format --check:** passed (no formatting issues)

---

## Self-Review

1. ✓ — Phase 4 of `internal/cycle/SKILL.md` rewritten to describe wave-based dispatch via shared task list with deps.
2. ✓ — Tests-at-wave-boundaries documented with concrete `run_ci_checks` invocation.
3. ✓ — In-cycle fix iteration scoped to the failing wave (max 3 rounds; abandon as `tests_unrecoverable` if not recovered).
4. ✓ — Owner from Phase 3 carries through as the implementer (skin in the game).
5. ✓ — Failure-mode section + before/after comparison present.
6. ✓ — pytest + ruff check + ruff format all green (see CI Results above).
7. ✓ — Minutes file written at `docs/kaizen/2026-05-23-cycle-10-2-minutes.md`.
8. ✓ — Exactly 2 modified files: `internal/cycle/SKILL.md` (Phase 4 rewritten) + `docs/kaizen/2026-05-23-cycle-10-2-minutes.md` (new).

---

## Notes

- The `run_ci_checks` call signature in the Phase 4 wave-boundary test block intentionally mirrors the Phase 5b call for consistency — same function, same pattern, two different trigger points.
- The `SendMessage`-based conflict resolution (replacing "mini-synthesis (one item)") is a deliberate simplification: it avoids spawning a full synthesis sub-process for small mid-implementation disagreements, while still having an escalation path to the lead.
- Open question (from seed design §5): should the Phase 4 wave-based dispatch produce per-wave commits or one cycle commit? Current answer (D3 from seed design): one cycle commit in Phase 5c. Phase 4 prose is explicit that tasks land in the working tree but are not committed until Phase 5c.
