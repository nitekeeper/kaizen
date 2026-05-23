---
description: Use when an agent has been routed from `internal/run/SKILL.md` to execute one improvement cycle in a prepared clone. Runs PM agenda → parallel pre-analysis → synthesis meeting → implementation → destructive check → tests → commit → minutes.
---

# internal/cycle

A single Kaizen improvement cycle. Mirrors atelier's `internal/self-improve/SKILL.md` Phase 1–5 structure with two differences:

- **No per-cycle push or merge.** The run-level orchestrator (`internal/run/SKILL.md`) pushes once at the end of all cycles.
- **Abandonment is structured, not a hard abort.** When the cycle cannot complete, return a structured outcome dict with `status="abandoned"` and a `reason` code; the orchestrator records it and continues to the next cycle.

The clone already exists when this skill is invoked — `internal/clone-target/SKILL.md` set it up. The run branch is already checked out — `internal/run/SKILL.md` created it. This skill operates inside that environment.

## Execution modes

Kaizen supports two agent coordination modes, selected via `--mode` on `kaizen:improve`:

| Mode | Mechanism | Env requirement |
|---|---|---|
| `subagent` (default) | Fire-and-forget `Agent` tool calls — one-shot dispatch, no shared state between agents | None |
| `team` | Persistent named team via `TeamCreate` / `SendMessage` / `TeamDelete` — agents carry context across multiple messages within the same cycle | `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` |

The mode does not change the cycle's logical structure (Phase 1–5). It only changes how agents are dispatched in Phase 2 (pre-analysis) and Phase 3 (synthesis meeting):

- **subagent mode:** each Phase 2 participant is a separate `Agent` call; Phase 3 synthesis happens in the orchestrating agent's context.
- **team mode:** `TeamCreate` opens a named team at cycle start; `SendMessage` briefs each participant; debate happens via `SendMessage` exchanges; `TeamDelete` closes the team at cycle end.

When `mode='team'` and `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` is absent, `scripts/team_executor.py::team_cycle_executor` raises `TeamToolsUnavailableError` before any clone work begins.

For `mode='team'`, the orchestrating agent provides a `TeamTools` wrapper
by subclassing `scripts.team_tools_wrapper.AgentTeamsWrapper` and
overriding `team_create` / `send_message` / `team_delete` to invoke the
real CC tools. Tests inject `RecordingWrapper` for harness coverage.

The dispatch templates the wrapper sends are exported from
`scripts.dispatch_templates` — each is a pure function with explicit
required-kwarg validation. Templates correspond 1:1 with the Phase 1-5c
dispatch points in `scripts.team_executor`.

## Inputs

- `clone_dir` (Path) — the experiment clone, already on the run branch with atelier seeded.
- `project` (dict) — the project row, including `read_paths`, `expert_roster`, `test_command`, `name`, `git_url`.
- `run_row` (dict) — the kaizen run row, including `id`, `branch`, `subject`.
- `cycle_n` (int) — 1-indexed cycle number within this run.
- `mode` (str, optional) — `'subagent'` (default) or `'team'`. Passed through from the run-level orchestrator.

## Outcome (return)

Return a dict:

```python
# Success
{
  "status": "success",
  "subject": "<cycle subject or None>",
  "commit_sha": "<sha>",
  "minutes_memex_slug": "kaizen:cycle:<run_id>-<cycle_n>",
  "participants": ["<agent_name>", ...],
}

# Abandoned
{
  "status": "abandoned",
  "subject": "<cycle subject or None>",
  "participants": ["<agent_name>", ...],
  "phase_reached": "agenda" | "meeting" | "implementation" | "test" | "review" | "push",
  "reason": "no_consensus" | "destructive_rejected" | "tests_unrecoverable" | "review_unrecoverable" | "other",
  "detail": "<free-text describing what was attempted and what blocked it>",
  "artifacts": ["<memex slug or path>", ...],
}
```

## Procedure

### Phase 1 — PM agenda

Read `internal/pm-agenda/SKILL.md` and follow its procedure with `(clone_dir, project, run_row, cycle_n)`. The result is:

- A markdown agenda block (PM Assessment if no `--subject` was provided, plus a numbered list of improvement questions)
- A resolved participant list (resolved from `project.expert_roster` via `internal/expert-roster/SKILL.md`)

Hold both in working context for the rest of the cycle.

If `internal/pm-agenda` returns no agenda items (e.g., the PM finds nothing worth proposing), abandon:

```python
{ "status": "abandoned", "phase_reached": "agenda",
  "reason": "other", "detail": "PM produced no agenda items for this cycle.",
  "participants": <resolved>, "artifacts": [], "subject": run_row["subject"] }
```

### Phase 2 — Parallel pre-analysis

Dispatch every participant in parallel (one agent per role from the resolved roster). Each agent independently:

1. Reads the files in `project["read_paths"]` that are relevant to their domain (no need to re-read every file).
2. Writes a structured proposal addressing the agenda items they have an opinion on:
   - **Finding** — specific files, patterns, problems they observed.
   - **Proposal** — concrete change and rationale.
   - **Risk classification** — destructive or non-destructive (per Kaizen's destructive_check categories).
   - **Anticipated conflicts** with other agents' likely positions.
   - **Touches** — files this proposal modifies if accepted.
   - **Reads** — files this proposal needs in a specific state (post-other-changes) before it can be applied.
   - **Likely depends on** — the proposing agent's best guess of which other agenda items must land first.

Collect every proposal before Phase 3 begins. Do not let one slow agent block the others — give each a bounded budget; if an agent fails to respond, drop them from this cycle's roster and note in the minutes.

The participants who complete Phase 2 carry through as teammates into Phase 3 — their pre-analysis context and skin-in-the-game ownership of their proposals are exactly what the synthesis meeting needs.

### Phase 3 — Synthesis meeting

Read `internal/synthesis-meeting/SKILL.md` and follow its procedure with the agenda items, the resolved participant list, and all collected proposals.

The meeting runs as a **Star → Mesh → Star** agent-teams pattern: the lead opens by broadcasting all proposals to every participant; teammates debate directly (mesh) to validate proposals, surface false positives, and detect ripple effects; the lead closes by writing a consolidated Decisions Log and an **Action Items DAG with wave assignments**.

The skill returns:

- A meeting minutes block (Discussion + Decisions Log + Action Items)
- An outcome signal: either `proceed` with at least one approved Action Item, or `abandon` with `reason=no_consensus`
- For `proceed`: a structured Action Items list where each item carries `id`, `description`, `touches`, `reads`, `owner`, `depends_on`, and `wave`

If outcome is `abandon`:

```python
{ "status": "abandoned", "phase_reached": "meeting",
  "reason": "no_consensus",
  "detail": "<summary of why every agenda item was dropped>",
  "participants": <resolved>, "artifacts": [<minutes slug if captured>],
  "subject": run_row["subject"] }
```

Otherwise continue with the Action Items DAG produced by the meeting. Phase 4 will use the `wave` and `depends_on` fields to coordinate parallel implementation.

### Phase 4 — Implementation (wave-based parallel dispatch)

The synthesis meeting (Phase 3) handed off a DAG of Action Items with `depends_on` set per task. Phase 4 consumes that DAG via the Agent Teams shared task list — execution is driven by the dependency graph, not an explicit wave loop.

#### Procedure

1. **Lead posts the DAG to the shared task list.** Each Action Item becomes a task with the columns from Phase 3 (`Touches`, `Reads`, `Owner`, `Depends on`). Tasks start in `pending` state.

2. **Teammates self-claim unblocked tasks.** A task is unblocked when all its `Depends on` predecessors are in `completed` state. File-locked claim semantics (per Agent Teams docs) prevent races when multiple teammates compete for the same task.

3. **Owner-driven implementation.** Each Action Item's `Owner` (assigned in Phase 3) is the teammate that claims and implements it. The agent who proposed the change is the agent applying it — skin in the game across phases.

4. **Tests run at wave boundaries.** After all tasks at topological level N complete (i.e. the "wave" closes), the lead runs:
   ```
   from pathlib import Path
   from scripts.ci_runner import run_ci_checks
   all_passed, results = run_ci_checks(Path(r'<clone_dir>'), '<project["test_command"]>')
   ```
   If `all_passed=False`: dispatch the wave's owners (and any test-focused experts in the roster) to fix the failing checks BEFORE wave N+1's tasks unblock. In-cycle fix iteration applies (max 3 fix rounds; abandon as `tests_unrecoverable` if not recovered).
   If `all_passed=True`: wave N+1's tasks (which depend on wave N) automatically unblock; teammates self-claim and continue.

5. **Mini-synthesis on mid-implementation conflicts.** If two teammates' work surfaces a conflict that wasn't caught in Phase 3 (e.g. an unforeseen ripple effect), they `SendMessage` each other to resolve. Lead intervenes if no resolution within 2 exchanges. Do not abandon the cycle for a resolvable per-file disagreement.

6. **Wave completion = all tasks claimed and completed.** No mid-wave commits; tasks land in the clone working tree but are not committed until Phase 5c. The cycle's tests run continuously at wave boundaries to catch regressions.

#### Failure modes

- **A task fails repeatedly** (test failures, claimed but completion stalled): the owner abandons it back to the task list with `status=pending` and a failure note. Lead reassigns or escalates.
- **DAG deadlock** (a task's `Depends on` references something that itself failed): break by either (a) marking the failed predecessor as `acceptable` per PM ruling, or (b) abandoning the cycle as `tests_unrecoverable`.

#### What changed from the pre-redesign Phase 4

| Before | After |
|---|---|
| Single implementer subagent ran all Action Items sequentially | Multiple teammates self-claim from shared task list with deps |
| Mid-cycle conflict escalated to "mini-synthesis (one item)" | Mid-cycle conflict resolved via teammate-to-teammate SendMessage |
| Test run once at end of cycle | Tests run at every wave boundary |
| Owner = same implementer for everything | Owner = the agent who proposed it in Phase 3 (skin in the game) |

### Phase 5a — Destructive check

```
python <kaizen-root>/scripts/destructive_check.py <clone_dir>
```

- Exit 0, empty list: no destructive changes — proceed to Phase 5b.
- Non-empty JSON list: for each entry, ask the user:

  > "Cycle N proposes a destructive change: [description] in [file]. Approve? (y/n)"

  - **Approved:** keep the change; re-run the check.
  - **Rejected:** revert that change in the clone working tree (`git checkout -- <file>` or undo the deletion), then re-run the check.

  Iterate until exit 0.

If, after all rejections, the working tree is clean (no remaining changes), abandon:

```python
{ "status": "abandoned", "phase_reached": "implementation",
  "reason": "destructive_rejected",
  "detail": "All proposed changes were classified destructive and rejected by the user.",
  "participants": <resolved>, "artifacts": [<minutes slug if captured>],
  "subject": run_row["subject"] }
```

### Phase 5b — Tests (with in-cycle fix iteration)

```
python3 -c "
from pathlib import Path
from scripts.ci_runner import run_ci_checks
all_passed, results = run_ci_checks(Path(r'<clone_dir>'), '<project[\"test_command\"]>')
for name, (ok, output) in results.items():
    print(f'{\"PASS\" if ok else \"FAIL\"}: {name}')
"
```

- **PASS (all checks green):** proceed to Phase 5c.
- **FAIL:**
  1. Capture the failing output for the agents to read.
  2. **Per-check routing**: when `results` contains failed checks, route by check name:
     - `tests` failure → dispatch the implementer agents from Phase 4 plus any test-focused experts. Diagnose the failure and apply a fix in the clone working tree.
     - `ruff_check` failure → dispatch a single style-focused agent (or the implementer); apply `ruff check --fix .` in the clone; recommit if changes were produced.
     - `ruff_format` failure → run `ruff format .` in the clone; recommit.
     - `lint_warning` → not a failure; surface the warning to the user but proceed.
  3. Re-run the CI checks. Iterate this fix-and-retest loop **within Phase 5b** — do NOT escalate a test failure to "next cycle." Multiple fix rounds happen in the same cycle.
  4. Bound the iteration: if after 3 fix attempts the suite is still red, OR if the agents conclude the failure is structural (test exposes a design flaw the proposed change cannot fix), abandon:

     ```python
     { "status": "abandoned", "phase_reached": "test",
       "reason": "tests_unrecoverable",
       "detail": "<summary of attempts and the final failure mode>",
       "participants": <resolved>, "artifacts": [<minutes slug>],
       "subject": run_row["subject"] }
     ```

  When abandoning at this phase, also revert the working tree (`git reset --hard HEAD`) so the next cycle starts clean.

### Phase 5b' — Independent Reviewers (parallel reviews + meeting + fix loop)

After Phase 5b's tests pass, the cycle does not yet commit. A new independent-reviewer phase runs to surface bugs, design issues, and false positives the implementers may have missed. Same shape as Phase 2 → Phase 3 but scoped to review.

#### Procedure

1. **Spawn independent reviewer teammates** (different from the Phase 2/4 implementers; drawn from the same `expert_roster` but a participant CANNOT review their own work). Different lenses: security, prompt-clarity, architecture, etc. Typically 2-4 reviewers per cycle.

   Pick the reviewer roster via the helper (enforces disjointness from Phase 4 implementers):

   ```python
   from scripts.reviewers import select_reviewers
   reviewers = select_reviewers(
       roster=project["expert_roster"],
       implementers=[<agent role ids that owned Phase 4 tasks>],
       n=3,
       preferred_lenses=["security", "architect", "prompt", "safety"],
   )
   ```

   If the helper raises `InsufficientRosterError`, escalate to the PM (the roster is too small for safe review — either expand the roster or abandon the cycle).

2. **Parallel reviews.** Each reviewer examines the post-Phase-4 diff (use `git diff HEAD~N..HEAD` or `git diff` against the cycle's starting commit) and produces a structured findings block:
   - **Issue** — what's wrong, with file:line
   - **Severity** — blocker / major / minor / nit
   - **Recommended fix** — concrete change
   - **Confidence** — high / medium / low (how sure they are the issue is real)

3. **Reviewer meeting (Star → Mesh → Star).** Mirrors Phase 3 but scoped to validating findings:
   - **Open (Star):** Lead `SendMessage`s each reviewer the consolidated raw findings + everyone else's findings.
   - **Debate (Mesh):** Reviewers `SendMessage` each other to validate each other's claims, weed out false positives (cross-confirmation: a finding survives only if another reviewer can confirm or it withstands challenge), calibrate severity, surface ripple effects between findings.
   - **Convergence:** Max 3 mesh exchanges per reviewer OR explicit "agreed" signals to lead.
   - **Close (Star):** Lead writes the **consolidated review report** — only peer-validated findings + agreed severity + recommended fixes.

4. **Fix loop.** The consolidated report drives a closed review-fix-review iteration:
   - Implementers (Owner from Phase 3 carries forward) fix all blocker + major issues; minor/nit may be deferred per PM ruling.
   - Reviewers re-examine the new diff.
   - New consolidated report produced.
   - If the new report has zero unresolved issues OR PM rules remaining issues acceptable → exit loop; proceed to Phase 5c.
   - Otherwise: another fix iteration.

   The iteration counter is mechanical — use the helper. The `pm_ruling_here` variable in the example is the PM's boolean acceptance ruling for this round's remaining issues — `True` when PM accepts the unresolved findings as known-and-acceptable, `False` otherwise. The orchestrator computes it after the reviewer meeting closes.

   ```python
   from scripts.fix_loop import (
       FixLoopState, Finding,
       start_iteration, record_findings, should_continue,
       build_abandonment_outcome, FixLoopExhausted,
   )

   state = FixLoopState()
   while True:
       try:
           n = start_iteration(state)  # raises FixLoopExhausted after 5
       except FixLoopExhausted:
           return build_abandonment_outcome(
               state, subject=run_row["subject"], participants=resolved,
           )
       # ... reviewer round produces `findings: list[Finding]` ...
       record_findings(state, findings)
       if not should_continue(state, pm_accepts_remaining=pm_ruling_here):
           break  # exit reason determined by should_continue's contract (zero blockers OR PM ruled acceptable)
   ```

   When `start_iteration` raises, the orchestrator returns the outcome from `build_abandonment_outcome` — which constructs the exact `review_unrecoverable` abandonment dict the orchestrator's allowlist guard (`scripts/run.py::orchestrate_run`, see Cycle 1) accepts.

   - **MAX 5 iterations.** If the loop exhausts with unresolved issues, abandon the cycle with `reason=review_unrecoverable`.

5. **Mini-synthesis for conflicting reviewers.** When two reviewers disagree on the same file (e.g. Security says "parameterize the SQL", Prompt Engineer says "remove the embedded SQL entirely"), they `SendMessage` each other directly to reconcile. Lead intervenes only if 3+ exchanges fail to resolve.

#### Abandonment (review_unrecoverable)

If the fix loop exhausts all 5 iterations with unresolved issues, abandon:

```python
{ "status": "abandoned", "phase_reached": "review",
  "reason": "review_unrecoverable",
  "detail": "<summary: iteration count, final unresolved issues, why fix loop couldn't converge>",
  "participants": <resolved>, "artifacts": [<minutes slug if captured>],
  "subject": run_row["subject"],
  # Structured fields (passed to record_abandonment/process_abandonment as kwargs):
  "review_iteration_count": <int 1..5>,
  "unresolved_findings": [{"reviewer": ..., "severity": ..., "finding": ..., "file_line": ...}, ...],
  "convergence_summary": "<why the fix loop couldn't converge>",
  "reviewer_attribution": {"<finding_id>": "<reviewer_role_id>", ...} }
```

The abandonment report MUST include:
- Iteration count actually run (e.g. 5)
- Final consolidated review report verbatim with all unresolved issues + severity
- Which reviewer flagged each issue
- Summary of why the fix loop couldn't converge (e.g. "issue X re-flagged in rounds 2/3/4 — implementer's fix didn't satisfy reviewer")
- Suggested next-session approach: pick up surgically, change approach, or accept partial work

When abandoning, also revert the working tree (`git reset --hard HEAD`) so the next cycle starts clean.

### Phase 5c — Commit

Compile the decisions and participants into the inputs the commit helper expects, then:

```
python3 -c "
from pathlib import Path
from scripts.cycle_git import commit_cycle
commit_cycle(
    clone_dir=Path(r'<clone_dir>'),
    cycle_n=<cycle_n>,
    decisions=['<d1>', '<d2>', ...],
    participants=['<agent name 1>', '<agent name 2>', ...],
    n_tests=<count from Phase 5b>,
    subject=<run_row['subject'] or 'PM-directed'>,
    minutes_rel_path='docs/kaizen/<YYYY-MM-DD>-cycle-<n>-minutes.md',
)
"
```

Capture the resulting commit sha (`git -C <clone_dir> rev-parse HEAD`).

Also write the full meeting minutes into the clone at the same relative path before committing — the commit message references it.

### Phase 5d — Minutes (committed in Phase 5c; cross-run capture deferred)

The cycle minutes are committed into the clone at `docs/kaizen/<YYYY-MM-DD>-cycle-<n>-minutes.md` during Phase 5c. The PR therefore preserves them in git history — that is the canonical store for cycle minutes.

The original spec also intended to capture minutes into Kaizen's own Memex wiki via `memex:run capture` for cross-run search. **This is currently deferred** — `memex:run` is a Claude Code skill, not a CLI binary, and a Python subprocess cannot invoke a Claude Code skill. Until a future architecture allows skill invocation from subprocess, cross-run Memex capture is a manual step the user can perform post-cycle:

```
# After the PR opens, from the kaizen repo root:
memex:run capture --id kaizen:cycle:<run_id>-<cycle_n> docs/kaizen/<YYYY-MM-DD>-cycle-<n>-minutes.md
```

Slug convention: `kaizen:cycle:<run_id>-<cycle_n>`.

Set `minutes_memex_slug = "kaizen:cycle:<run_id>-<cycle_n>"` in the cycle return dict so the orchestrator surfaces it in the run summary — the slug is the intended identifier even though capture is manual.

### Return outcome

Return the success dict described under "Outcome (return)" with the slug, commit sha, and resolved participant names.

## Hard rules

- **In-cycle test fix iteration is mandatory.** Do not escalate a test failure to "next cycle" until the agents have actually attempted to repair it (typically up to 3 rounds).
- **Abandonment is structured.** Always return a dict with a `reason` from the five named codes (`no_consensus`, `destructive_rejected`, `tests_unrecoverable`, `review_unrecoverable`, `other`). The orchestrator depends on this for the abandonment report.
- **Working tree must be clean at cycle end** — committed (success path) or reset (abandon path). The next cycle inherits the same clone; an unclean tree leaks state.
- **Unanimous consensus is required** (synthesis meeting). Anything less drops the item; if every item drops, the cycle abandons.
- **Destructive changes require explicit user approval** — never bypass the destructive check, even when the user has pre-authorized other parts of the flow.
