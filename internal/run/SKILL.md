---
description: Use when an agent has been routed from `kaizen:improve` to run the full multi-cycle improvement flow. Loads or registers the project, clones the target, executes N cycles, opens one bundled PR, and tears down the clone.
---

# internal/run

N-cycle orchestration. This procedure is the entry point that `skills/improve/SKILL.md` routes to. It coordinates project lookup, clone setup, the cycle loop, push, PR open, and cleanup.

The Python helper `scripts/run.orchestrate_run()` exists and encodes this exact flow end-to-end — but it requires a `cycle_executor` callable to run real agent work, and the real cycle executor lives in `internal/cycle/SKILL.md` (prose, not Python). So this procedure walks each step explicitly and the agent calls the matching script function at each one.

## Inputs

- `git_url` (str, required)
- `cycles_requested` (int, default 1)
- `subject` (str | None, default None)

## Procedure

### Step 1 — Parse and validate

Confirm `git_url` is a valid git URL form and `cycles_requested` is a positive integer. Record the kaizen DB path as `.ai/memex.db` (default) and the kaizen root as the directory containing `scripts/`.

### Step 2 — Get or register the project

Read `internal/project/SKILL.md` and follow its `get-or-register` operation with `git_url`. The result is a project dict with at minimum `id`, `name`, `git_url`, `base_branch`, `test_command`, `read_paths`, `expert_roster`, `language`.

If the user aborts the registration prompt, stop the run cleanly — do not create a run row.

### Step 3 — Clone + seed the target

Read `internal/clone-target/SKILL.md` and follow its `setup` operation with `git_url`. The result is `clone_dir` (a `Path`). The clone now has:

- The target repo checked out on its base branch
- Atelier's full schema + role roster seeded into `<clone>/.ai/memex.db`
- `<clone>/.ai/wiki/` directory present

### Step 3.5 — Build code-nav graph (best-effort, auto-skip)

Build an AST-only code-navigation graph of the freshly-seeded clone so Phase 2 recon agents can navigate the graph instead of grep + full-file reads:

```
python3 scripts/codegraph_recon.py build <clone_dir> <git_url>
```

This is **best-effort and ON by default.** It prints a JSON status and exits 0 even when it skips. It auto-skips (logging one stderr note, continuing the run) whenever `graphify` is not on PATH, memex >= 2.9.0 is not installed, the build/ingest fails, or `KAIZEN_CODEGRAPH` is set to `0`/`false`/`no`/`off`. It never writes into the PR diff — the `graphify-out/` artifact is removed from the clone after ingest. The graph is ingested into memex's `~/.memex/code_graph.db`, keyed by `owner/repo`.

`scripts.run.orchestrate_run` performs this step automatically between seed and branch (it never raises, so no try/suppress is needed) and threads the built-vs-skipped signal to Phase 2 via the `KAIZEN_CODEGRAPH_AVAILABLE` env var. When hand-driving the steps, **record whether the graph was built or skipped** (parse the printed `status`): Phase 2 agents only invoke the query CLI when the graph is available, and fall back to grep when it was skipped.

### Step 4 — Create the run branch

Compute and check out the kaizen branch in the clone:

```
python3 -c "
from pathlib import Path
from scripts.cycle_git import create_branch
print(create_branch(Path(r'<clone_dir>'), '<subject-or-empty>'))
"
```

(Pass `subject` literally; the helper slugifies it or substitutes `pm-directed` when None/empty.) Capture the printed branch name.

### Step 5 — Create the run row

Read `internal/run-record/SKILL.md` and follow its `create` operation with `(project_id=project["id"], branch=<branch from step 4>, cycles_requested, subject)`. The result is the run dict; record `run_id = run["id"]`.

### Step 6 — Cycle loop (skip-and-continue)

> **Team mode note.** When `mode='team'`, `scripts.run.orchestrate_run` requires a `tools_provider=Callable[[Path, dict, dict, int], TeamTools]` kwarg. It is invoked once per cycle to build the per-cycle `TeamTools` wrapper (typically an `AgentTeamsWrapper` subclass) and the wrapper is threaded into `team_cycle_executor` as a keyword-only `tools=` argument. Without it, `orchestrate_run` raises `ValueError` containing both `tools_provider` and `mode='team'` BEFORE any clone / seed / branch / run-row side effect — i.e. the run fails fast and leaves no garbage. Subagent mode silently ignores `tools_provider`. See `internal/cycle/SKILL.md` for the wrapper pattern and `scripts/team_tools_wrapper.py::AgentTeamsWrapper` for the base class.

For each `cycle_n` in `1..cycles_requested`:

1. Read `internal/cycle/SKILL.md` and follow its procedure with context `(clone_dir, project, run_row, cycle_n)`. The skill returns an outcome dict.
2. **If `outcome["status"] == "success"`:** call

   ```
   python3 -c "
   from scripts.cycle import record_cycle_success
   record_cycle_success(
       db_path='.ai/memex.db',
       run_id=<run_id>,
       cycle_n=<cycle_n>,
       subject=<outcome['subject']>,
       commit_sha=<outcome['commit_sha']>,
       minutes_memex_slug=<outcome['minutes_memex_slug']>,
       started_at=<cycle_started ISO timestamp>,
   )
   "
   ```

   Increment a running `cycles_succeeded` counter.
3. **If `outcome["status"] == "abandoned"`:** record the cycle row, then write the report:

   ```
   python3 -c "
   from scripts.cycle import record_cycle_abandoned
   row = record_cycle_abandoned(
       db_path='.ai/memex.db',
       run_id=<run_id>,
       cycle_n=<cycle_n>,
       subject=<outcome['subject']>,
       started_at=<cycle_started ISO timestamp>,
   )
   print(row['id'])
   "
   ```

   Then read `internal/abandonment-report/SKILL.md` and follow its procedure with `(project, run_id, cycle_id=<row id from above>, cycle_n, subject, participants, phase_reached, reason, detail, artifacts, review_iteration_count, unresolved_findings, convergence_summary, reviewer_attribution)` from `outcome`. The last 4 fields are only present when `reason='review_unrecoverable'` (Phase 5b' fix-loop exhaustion) — for every other abandonment reason they should be omitted (or passed as `None`), and the rendered report falls back to the legacy shape.

   Increment a running `cycles_abandoned` counter. **Do NOT stop the loop.**
4. **If `outcome["status"]` is anything else:** abort the run with a loud error — the cycle executor returned malformed output.

### Step 7 — Push the run branch

```
python3 -c "
from pathlib import Path
from scripts.cycle_git import push_branch
push_branch(Path(r'<clone_dir>'), '<branch>')
"
```

If the push raises (network down, permissions, branch protection), do NOT delete the clone. Finalize the run with `status='failed'` and `pr_url=None` (see step 9), surface the git error to the user, and stop. The clone is preserved so the user can recover manually.

### Step 8 — Open the PR

Read `internal/open-pr/SKILL.md` and follow its procedure with `(run_id, clone_dir)`. Capture the returned PR URL.

### Step 9 — Finalize the run

Read `internal/run-record/SKILL.md` and follow its `finalize` operation with `(run_id, cycles_succeeded, cycles_abandoned, pr_url=<from step 8>, status='complete')`.

If step 7 failed, run finalize with `status='failed'` and `pr_url=None` instead, then skip step 8 and step 10.

### Step 10 — Tear down the clone

Teardown is **unconditional** — it runs after PR open regardless of CI outcome. The clone is ephemeral; the PR branch on GitHub is the recovery artifact for any post-merge issues. Step 10.5 (which follows) reports CI status to the user but cannot — and is not designed to — affect this teardown.

Read `internal/clone-target/SKILL.md` and follow its `teardown` operation with `clone_dir`.

### Step 10.5 — Report CI status (informational only)

This step runs AFTER teardown (Step 10) and is **informational only**. The poll's return value is printed for the user but does not control teardown, the run row, or any downstream behaviour. See `scripts/pr.py::wait_and_report_ci` for the failure-mode rationale (Safety hard-stop in kaizen#17 against conditional teardown).

After teardown completes, poll `gh pr checks` to surface the CI outcome to the user. This step is informational only — it does NOT control teardown (which already happened in Step 10).

```python
from scripts.pr import wait_and_report_ci
status_line = wait_and_report_ci(pr_url, timeout_seconds=120)
print(status_line)
```

The function returns one of:
- `"✓ CI green on <pr_url> ..."` — all checks passed.
- `"✗ CI failing on <pr_url> ..."` — the PR branch is the recovery artifact (the clone is gone by design; CI failures recover by checking out the PR branch fresh).
- `"⌛ CI did not complete within 120s ..."` — check manually.

Surface the line to the user verbatim in the final summary.

### Step 11 — Surface the summary

Return / print to the caller (`skills/improve/SKILL.md`):

```
Run <run_id> complete.
  Branch:    <branch>
  PR:        <pr_url or "not opened — push failed">
  CI status: <line from Step 10.5>
  Cycles:    <S> succeeded / <A> abandoned out of <N> requested
  Project:   <project name> (<git_url>)
Abandonment memex slugs (read with `memex ask <slug>`):
  - <slug 1>
  - <slug 2>
Cycle minutes slugs:
  - <slug 1>
  - ...
```

## Skip-and-continue policy

A cycle that abandons (no_consensus, destructive_rejected, tests_unrecoverable, other) writes its abandonment report and the loop continues. The next cycle runs from the same clone, on the same branch, with a clean working tree (the abandoning cycle should have left no staged changes — see `internal/cycle/SKILL.md`'s cleanup rule).

Only these conditions halt the run before the cycle count is exhausted:

- The user aborts registration in step 2.
- A cycle returns a malformed outcome (step 6, neither "success" nor "abandoned").
- The push in step 7 fails (clone preserved for recovery).

In every other case the loop runs to `cycles_requested` and the PR is opened — even when all cycles were abandoned. Per design §3.3, an all-abandoned run still opens a PR (body lists the reports; no code commits). The user can review and decide whether to retry.

## Hard rules

- Never delete the clone before the PR opens. The user must be able to inspect the clone if PR open fails.
- Never silently swallow an abandonment. Every abandoned cycle produces a report row + memex capture.
- The run branch is created exactly once (step 4). All successful cycles commit onto it.
- Push happens exactly once (step 7), after the cycle loop. Per-cycle push is out of scope.
