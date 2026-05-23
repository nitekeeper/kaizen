# Kaizen Phase Redesign — Agent Teams + Waves + Independent Reviewers

**Status:** seed design — kaizen-on-kaizen run 10 will iterate on this in detail.
**Branch:** `agent-team` (long-lived; merges to main after dogfood validation).
**Date:** 2026-05-23.

## Motivation

The 9 kaizen runs landed today validated the orchestration end-to-end but surfaced three structural improvements:

1. **Phase 3 (synthesis meeting) is degenerate.** Today the PM single-handedly reads N parallel proposals and writes the Decisions Log + Action Items. No agent-to-agent debate. Conflicts get resolved silently in the PM's head (e.g. run 4 Backend's Option B vs Architect's Option A). Cross-agent validation only happened by accident (run 4's v1.0.13 schema mismatch caught by 3 independent agents).

2. **Phase 4 (implementation) is single-implementer.** The spec says "Each agent listed in an Action Item's 'Assigned to' column applies their changes" — implying parallel multi-agent implementation. Today's practice collapses to one implementer subagent doing all Action Items sequentially. Parallelism opportunity lost.

3. **No independent reviewer phase.** The personal-rules convention says "spawn a separate reviewer subagent after every working subagent." Today this is applied ad-hoc on some PRs (e.g. SDET-1 reviewed kaizen#17) but is not codified in the cycle structure. There is no place in the cycle SKILL.md where reviewers run.

This redesign addresses all three by introducing:

- **Agent Teams** as the substrate (replacing one-shot subagent dispatches with persistent multi-task teammates).
- **Waves** as the Phase 4 parallelism primitive (DAG of Action Items grouped into independent waves).
- **Phase 5b' Reviewers** as a new cycle sub-phase (parallel reviewers → reviewer meeting → consolidated report → fix loop).

## What stays the same

| Phase | Status |
|---|---|
| Phase 1 — PM agenda | No change |
| Phase 2 — Parallel pre-analysis | No change in substance; participants now become persistent teammates that carry through the cycle |
| Phase 5a — Destructive check | No change |
| Phase 5b — Tests (with in-cycle fix iteration) | No change; remains a single cycle-end run |
| Phase 5c — Commit | No change |
| Phase 5d — Minutes | No change (Option C — manual memex capture documented but not auto-invoked) |

## What changes

### Phase 3 — Synthesis meeting (redesigned)

**Today:** PM reads parallel proposals, writes Decisions Log + Action Items alone.

**Tomorrow:** Agent Teams meeting following the Star → Mesh → Star pattern:

1. **Open (Star):** PM/lead `SendMessage`s each participant the agenda + everyone's Phase 2 proposals.
2. **Debate (Mesh):** Teammates `SendMessage` each other directly to validate proposals, surface false positives, detect ripple effects, debate conflicting recommendations. Lead observes via idle notifications.
3. **Convergence:** Bounded by max-rounds (3 exchanges per teammate) OR explicit "I agree" signals back to lead.
4. **Close (Star):** Lead writes the consolidated meeting output: Decisions Log + Action Items DAG with waves + ripple-effect notes. Posted to the shared task list.

**New responsibilities for the meeting** (beyond today's "reach unanimous consent"):

- **Validate proposals** — catch false positives (e.g. claims based on stale docs vs actual code; run 6 dev-qa hard-stop was exactly this).
- **Detect ripple effects** — task A affecting task B across files; surface as explicit dependencies or new Action Items.
- **Build the DAG** — Action Items grouped into waves by topological levels of the dependency graph.

**New Action Items table format:**

| # | Action | Touches | Reads | Owner | Depends on | Wave |
|---|---|---|---|---|---|---|

- **Touches**: files the item modifies
- **Reads**: files the item depends on existing in a particular state (post-other-changes)
- **Owner**: assigned teammate (carries through to Phase 4 implementation — skin in the game)
- **Depends on**: predecessor Action Item IDs
- **Wave**: derived from topological level

**Validation gates before the meeting locks in:**

- DAG is acyclic
- No file contention within a wave (two items in same wave touching same file → push one to later wave)
- All `Reads` are satisfiable (file exists in codebase OR produced by earlier wave)
- No orphan dependencies (item depends on something nobody proposed)

**Phase 2 proposal template extended:**

Each agent's Phase 2 proposal gains:
- **Touches** — files this proposal modifies if accepted
- **Reads** — files this proposal needs in a specific state
- **Likely depends on** — proposing agent's best guess of which other agenda items must land first

### Phase 4 — Implementation (redesigned)

**Today:** Single implementer subagent runs all Action Items sequentially.

**Tomorrow:** Wave-based parallel dispatch via the team's shared task list.

1. The meeting (Phase 3) posts the entire Action Items DAG to the team's shared task list with `depends_on` set per task.
2. Teammates self-claim unblocked tasks (those with all predecessors completed). File-locked claim prevents races.
3. Each Action Item's `Owner` carries forward — the agent who proposed/owns it in the meeting is the one implementing it.
4. As tasks complete, dependents unblock automatically. The orchestrator doesn't drive the wave loop explicitly — the dependency graph drives execution.
5. Tests run at wave boundaries (G5b.1) — between Wave N and Wave N+1, run `ci_runner.run_ci_checks` against the working tree. If a wave's tests fail, dispatch the wave's owners to fix before allowing Wave N+1 to start.

### NEW Phase 5b' — Independent Reviewers

**Today:** No formal reviewer phase. Ad-hoc reviewer subagents on some PRs (personal-rules convention).

**Tomorrow:** New sub-phase after Phase 5b. Same shape as Phase 2 → Phase 3:

1. **Parallel reviews:** Spawn N independent reviewer teammates (different from the implementers; different lenses: security, prompt-clarity, architecture). Each reviewer examines the post-Phase-4 diff and produces structured findings.
2. **Reviewer meeting:** Reviewers convene as a team (Star → Mesh → Star) to:
   - Debate findings
   - Validate each other's claims (cross-confirmation; weed out false positives)
   - Calibrate severity
   - Produce a **consolidated review report** — findings survive only if peer-validated.
3. **Fix loop:** The consolidated report drives a closed review-fix-review loop:
   - Implementers fix all issues from the report
   - Reviewers re-examine
   - Repeat
   - **Max 5 iterations.** If exhausted with unresolved issues, abandon as `reason=review_unrecoverable`.
4. **Termination:** Latest reviewer meeting produces zero issues OR PM rules remaining issues acceptable.

**Mini-synthesis** when two reviewers disagree on the same file (e.g. Security says "parameterize SQL", Prompt Engineer says "remove embedded SQL entirely"): scoped Phase-3-style reconciliation BEFORE dispatching the fix.

**Abandonment report MUST include** (when `review_unrecoverable`):
- Iteration count actually run (e.g. 5)
- Final consolidated review report verbatim with all unresolved issues + severity
- Which reviewer flagged each issue
- Summary of why the fix loop couldn't converge (e.g. "issue X re-flagged in rounds 2/3/4 — implementer's fix didn't satisfy reviewer")

That way the next session can pick up surgically or change approach.

## New abandonment reason

Add `review_unrecoverable` to the `abandonments.reason` CHECK constraint.

Migration: `migrations/002_review_unrecoverable.sql` — `ALTER TABLE abandonments` not directly possible with SQLite CHECK constraints; will require a table recreation with the updated constraint. Schema migration following the existing pattern in `001_kaizen_schema.sql`.

Updated reasons list: `no_consensus`, `destructive_rejected`, `tests_unrecoverable`, `review_unrecoverable`, `other`.

## Risk classification

**Mostly NON-DESTRUCTIVE** — prose-only changes to `internal/cycle/SKILL.md` + `internal/synthesis-meeting/SKILL.md`. One schema migration adding a reason. No removal of existing functionality.

## Bootstrap plan

This redesign lands using the **CURRENT** kaizen (still on subagent model) running against itself. After it merges, a **new Claude Code session with tmux + `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`** will exercise the new flow for the first time.

The new Phase 3 / Phase 4 / Phase 5b' prose changes only take effect on the NEXT kaizen run after this PR merges — there is no risk of changing-the-rules-mid-game.

## Cycle plan for run 10 (kaizen-on-kaizen)

| Cycle | Scope | Files | Risk |
|---|---|---|---|
| 1 | Phase 3 redesign | `internal/synthesis-meeting/SKILL.md` + `internal/pm-agenda/SKILL.md` (Phase 2 template extension) + `internal/cycle/SKILL.md` Phase 3 section | NON-DESTRUCTIVE |
| 2 | Phase 4 redesign | `internal/cycle/SKILL.md` Phase 4 section (wave-based dispatch + test-at-wave-boundaries) | NON-DESTRUCTIVE |
| 3 | Phase 5b' new + abandonment reason | `internal/cycle/SKILL.md` (new sub-phase) + `internal/abandonment-report/SKILL.md` (new reason in enum) + `migrations/002_review_unrecoverable.sql` + `scripts/abandonment.py` (test new reason works) + tests | Mostly NON-DESTRUCTIVE (one migration) |
| 4 (optional) | Polish + design doc finalization | This file + any integration polish | NON-DESTRUCTIVE |

## Out of scope for this redesign

- **Atelier-side Agent Teams adoption** — separate effort; atelier's `internal/synthesis-meeting/SKILL.md` and `~/.memex/agents.db` → `~/.claude/agents/<role-id>.md` export are atelier's responsibility, not kaizen's.
- **tmux preflight detection** — handled separately (GR.1); not blocking.
- **Token cost optimization** — accepted as the cost of correctness (concern #1 from earlier discussion).
- **Cross-cycle agent context** — Agent Teams clears the task list at team teardown; cross-cycle continuity has to be re-established via the spawn prompt each cycle. Accepted limitation.

## Test strategy

- **Prose changes** validated by `tests/test_skill_frontmatter.py` (structural conformance) — no semantic-test exists today.
- **Migration** validated by `tests/test_migrate.py` (or a new test that applies the migration to a tmp DB and asserts the `abandonments.reason` CHECK allows `review_unrecoverable`).
- **`scripts/abandonment.py`** — `record_abandonment` should accept the new reason; add a test.
- **Dogfood** — the next kaizen run (in a new session with tmux + Agent Teams enabled) is itself the integration test. It exercises the new Phase 3 meeting, the new Phase 4 wave dispatch, and the new Phase 5b' reviewer phase end-to-end.

## Open questions for the kaizen-on-kaizen meeting agents to debate

1. Should reviewer teammates be drawn from the same `expert_roster` as the meeting participants, or a separate `reviewer_roster`? Current proposal: same roster, but a participant can't review their own work.
2. How does the lead detect convergence in the Mesh phase of a meeting? Time-based, round-based, or explicit "I agree" signals? Current proposal: max 3 mesh rounds OR explicit agreement, whichever first.
3. Does the Phase 4 wave-based dispatch produce per-wave commits or one cycle commit? Current proposal: one cycle commit (PR-readability over git granularity).
4. Should `mini-synthesis` inside Phase 5b' be a formal sub-step or just a normal `SendMessage` conversation between conflicting reviewers? Current proposal: normal conversation; only escalate to explicit mini-synthesis when 3 reviewers triangulate.
5. Should the abandonment report's "unresolved issues" section be machine-readable JSON or human-readable markdown? Current proposal: markdown (human-readable, matches existing report format).
