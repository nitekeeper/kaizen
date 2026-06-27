# Prose-transport opt-out procedure (KAIZEN_TRANSPORT=prose)

This is the explicit `KAIZEN_TRANSPORT=prose` opt-out path for Phase 4 (prose) and Phases 5a/5b/5b-prime/5c. It is read ONLY when `KAIZEN_TRANSPORT=prose` is set; the default host path never reads it. The host-path Phase 4 fork stays in `internal/cycle/SKILL.md`.

The rest of this Phase-4 section (and Phases 5a–5c) is the explicit `KAIZEN_TRANSPORT=prose` opt-out path (no longer the default — see the transport fork above).

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

> **Skip on the host path.** When `KAIZEN_TRANSPORT=host`, Phases 5a–5c are subsumed by `scripts.host_cycle_entry` (see the Phase 4 transport fork) — do NOT run them by prose. This section is the explicit `KAIZEN_TRANSPORT=prose` opt-out path only (no longer the default).

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

> **Skip on the host path.** When `KAIZEN_TRANSPORT=host`, the host engine ALREADY ran the post-Phase-4 CI-mirror gate in-process (see the Phase 4 transport fork) — do NOT run Phase 5b by prose. This section is the explicit `KAIZEN_TRANSPORT=prose` opt-out path only (no longer the default).

```
PYTHONPATH=. python3 -c "
from pathlib import Path
from scripts.ci_runner import run_ci_checks
all_passed, results = run_ci_checks(Path(r'<clone_dir>'), '<project[\"test_command\"]>')
for name, r in results.items():
    label = r['status'].upper()           # PASS / FAIL / SKIP
    extra = f' ({r[\"reason\"]})' if 'reason' in r else ''
    print(f'{label}{extra}: {name}')
"
```

Each check returns ``{"status": "pass" | "fail" | "skip", "output": <stdout+stderr>, "reason": <named code, optional>}``. ``all_passed`` is ``True`` iff every check's status is ``pass`` or ``skip`` — a ``skip`` is never a failure. Keys in ``results``:

| Key | When present | Status meaning |
|---|---|---|
| `tests` | always | `pass` / `fail` from the project's test_command |
| `ruff_check`, `ruff_format` | when [tool.ruff] / ruff.toml is present in the target | `pass` / `fail` |
| `lint_warning` | when no ruff config is detected | always `skip` with reason `no_ruff_config` |
| `bandit` | always | `pass` / `fail` if [tool.bandit] / .bandit / bandit.yaml is present; `skip` with reason `no_bandit_config` otherwise |
| `pip_audit` | always | `pass` / `fail` if any `.github/workflows/*.yml` mentions `pip-audit`; `skip` with reason `no_pip_audit_in_workflows` or `opted out via KAIZEN_SKIP_PIP_AUDIT` |

- **PASS (all checks green or skipped):** proceed to Phase 5c.
- **FAIL** (any check has `status == "fail"`):
  1. Capture the failing output for the agents to read.
  2. **Per-check routing**: when `results` contains failed checks, route by check name:
     - `tests` failure → dispatch the implementer agents from Phase 4 plus any test-focused experts. Diagnose the failure and apply a fix in the clone working tree.
     - `ruff_check` failure → dispatch a single style-focused agent (or the implementer); apply `ruff check --fix .` in the clone; recommit if changes were produced.
     - `ruff_format` failure → run `ruff format .` in the clone; recommit.
     - `bandit` failure → check `results["bandit"]["reason"]` first:
       - `bandit_findings` (exit 1) → real SAST hits; dispatch a security-focused agent (or the implementer) to read `output` and apply a fix in the clone — either patch the offending code, or add a justified `# nosec` / `[tool.bandit]` skip with an inline rationale. Recommit.
       - `bandit_config_error` (exit 2) → Bandit config-file invalid (rc=2 means YAML parse error / unknown directive in `.bandit` / `bandit.yaml` / `pyproject.toml [tool.bandit]`, NOT a generic scanner crash). Treat as a Bandit-configuration bug rather than a code finding; dispatch an implementer to repair the config or pin a Bandit version. Do NOT silence by adding skips.
       - `bandit_binary_missing` → Bandit isn't installed in the local environment. Install Bandit and re-run; if the local install can't be fixed, surface to the user — never silence by removing `[tool.bandit]` from the target.
     - `pip_audit` failure → check `results["pip_audit"]["reason"]`:
       - `pip_audit_exit_*` → real SCA hits or a pip-audit error; dispatch a security/dependency-focused agent to read `output`, upgrade the affected package (or pin a fixed version), and recommit. If the project documents the vulnerability as accepted-risk, the agent must record that decision in the cycle minutes — do NOT silently ignore.
       - `pip_audit_binary_missing` → install pip-audit OR set `KAIZEN_SKIP_PIP_AUDIT=1` for this run (offline mode); surface to the user.
     - `lint_warning` → status is `skip`, never a fail. Do NOT route as a failure. Surface the warning to the user once and proceed.
     - `bandit` / `pip_audit` with `status == "skip"` → not failures (the target doesn't opt in, or pip-audit is opted out via `KAIZEN_SKIP_PIP_AUDIT=1`). Proceed.
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

> **Skip on the host path.** When `KAIZEN_TRANSPORT=host`, the host engine ALREADY ran this review→fix loop in-process (see the Phase 4 transport fork) — do NOT run Phase 5b' by prose, or you double-review. This section is the explicit `KAIZEN_TRANSPORT=prose` opt-out path only (no longer the default).

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

> **Skip on the host path.** When `KAIZEN_TRANSPORT=host`, the host engine ALREADY committed the merged work internally (`commit_cycle_and_sha`) and returned the real `commit_sha` in the outcome dict — do NOT call `commit_cycle` again, or you double-commit (F3). This section is the explicit `KAIZEN_TRANSPORT=prose` opt-out path only (no longer the default).

Compile the decisions and participants into the inputs the commit helper expects, then:

```
PYTHONPATH=. python3 -c "
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

Do **NOT** write the minutes file into the clone before committing — `commit_cycle` runs `git add -A` in the clone, so a minutes file there would land in the target repo's PR diff. Minutes are process artifacts whose canonical store is **Memex** (Phase 5d); the `minutes_rel_path` in the commit message is a reference label naming the Memex-captured artifact, not a file in the commit.
