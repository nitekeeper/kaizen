---
description: Use when an agent has been routed from `internal/run/SKILL.md` to execute one improvement cycle in a prepared clone. Runs PM agenda → parallel pre-analysis → synthesis meeting → implementation → destructive check → tests → commit → minutes.
---

# internal/cycle

A single Kaizen improvement cycle. Mirrors atelier's `internal/self-improve/SKILL.md` Phase 1–5 structure with two differences:

- **No per-cycle push or merge.** The run-level orchestrator (`internal/run/SKILL.md`) pushes once at the end of all cycles.
- **Abandonment is structured, not a hard abort.** When the cycle cannot complete, return a structured outcome dict with `status="abandoned"` and a `reason` code; the orchestrator records it and continues to the next cycle.

The clone already exists when this skill is invoked — `internal/clone-target/SKILL.md` set it up. The run branch is already checked out — `internal/run/SKILL.md` created it. This skill operates inside that environment.

## Inputs

- `clone_dir` (Path) — the experiment clone, already on the run branch with atelier seeded.
- `project` (dict) — the project row, including `read_paths`, `expert_roster`, `test_command`, `name`, `git_url`.
- `run_row` (dict) — the kaizen run row, including `id`, `branch`, `subject`.
- `cycle_n` (int) — 1-indexed cycle number within this run.

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
  "phase_reached": "agenda" | "meeting" | "implementation" | "test",
  "reason": "no_consensus" | "destructive_rejected" | "tests_unrecoverable" | "other",
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

Collect every proposal before Phase 3 begins. Do not let one slow agent block the others — give each a bounded budget; if an agent fails to respond, drop them from this cycle's roster and note in the minutes.

### Phase 3 — Synthesis meeting

Read `internal/synthesis-meeting/SKILL.md` and follow its procedure with the agenda items + all collected proposals. The skill returns:

- A meeting minutes block (Discussion + Decisions Log + Action Items)
- An outcome signal: either `proceed` with at least one approved Action Item, or `abandon` with `reason=no_consensus`

If outcome is `abandon`:

```python
{ "status": "abandoned", "phase_reached": "meeting",
  "reason": "no_consensus",
  "detail": "<summary of why every agenda item was dropped>",
  "participants": <resolved>, "artifacts": [<minutes slug if captured>],
  "subject": run_row["subject"] }
```

Otherwise continue with the list of approved Action Items.

### Phase 4 — Implementation

Each agent listed in an Action Item's "Assigned to" column applies their changes directly to `clone_dir`. Agents may use any tooling appropriate — the only constraint is that the changes must land in the clone's working tree.

Coordinate to avoid stomping on the same file in incompatible ways. If a conflict surfaces mid-implementation, escalate to a mini-synthesis (one item) before proceeding — do not abandon the whole cycle for a per-file disagreement that can be resolved.

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
python -c "
from pathlib import Path
from scripts.test_runner import run_tests_in_clone
passed, count = run_tests_in_clone(Path(r'<clone_dir>'), '<project[\"test_command\"]>')
print('PASS' if passed else 'FAIL', count)
"
```

- **PASS:** record `n_tests = count`, proceed to Phase 5c.
- **FAIL:**
  1. Capture the failing output for the agents to read.
  2. Dispatch the relevant agents (typically the implementers from Phase 4 plus any test-focused experts in the roster) to read the failure, propose a fix, and apply it to the clone.
  3. Re-run the test command. Iterate this fix-and-retest loop **within Phase 5b** — do NOT escalate a test failure to "next cycle." Multiple fix rounds happen in the same cycle.
  4. Bound the iteration: if after 3 fix attempts the suite is still red, OR if the agents conclude the failure is structural (test exposes a design flaw the proposed change cannot fix), abandon:

     ```python
     { "status": "abandoned", "phase_reached": "test",
       "reason": "tests_unrecoverable",
       "detail": "<summary of attempts and the final failure mode>",
       "participants": <resolved>, "artifacts": [<minutes slug>],
       "subject": run_row["subject"] }
     ```

  When abandoning at this phase, also revert the working tree (`git reset --hard HEAD`) so the next cycle starts clean.

### Phase 5c — Commit

Compile the decisions and participants into the inputs the commit helper expects, then:

```
python -c "
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

### Phase 5d — Capture minutes to Kaizen's memex

The cycle minutes belong in Kaizen's own wiki (not the target's), so future runs can search across past cycles.

Slug: `kaizen:cycle:<run_id>-<cycle_n>`

Invoke `memex capture` against the Kaizen repo:

```
cd <kaizen-root>
memex capture --id kaizen:cycle:<run_id>-<cycle_n>
# stdin: the full minutes markdown
```

If `memex` is not on PATH or capture fails, do not abandon the cycle — log a warning and continue with `minutes_memex_slug=None`. The cycle still succeeded; the minutes can be re-ingested manually later.

### Return outcome

Return the success dict described under "Outcome (return)" with the slug, commit sha, and resolved participant names.

## Hard rules

- **In-cycle test fix iteration is mandatory.** Do not escalate a test failure to "next cycle" until the agents have actually attempted to repair it (typically up to 3 rounds).
- **Abandonment is structured.** Always return a dict with a `reason` from the four named codes (`no_consensus`, `destructive_rejected`, `tests_unrecoverable`, `other`). The orchestrator depends on this for the abandonment report.
- **Working tree must be clean at cycle end** — committed (success path) or reset (abandon path). The next cycle inherits the same clone; an unclean tree leaks state.
- **Unanimous consensus is required** (synthesis meeting). Anything less drops the item; if every item drops, the cycle abandons.
- **Destructive changes require explicit user approval** — never bypass the destructive check, even when the user has pre-authorized other parts of the flow.
