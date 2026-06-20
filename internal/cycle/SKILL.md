---
description: Use when an agent has been routed from `internal/run/SKILL.md` to execute one improvement cycle in a prepared clone. Runs PM agenda → parallel pre-analysis → synthesis meeting → implementation → destructive check → tests → commit → minutes.
---

# internal/cycle

A single Kaizen improvement cycle. Mirrors atelier's `internal/self-improve/SKILL.md` Phase 1–5 structure with two differences:

- **No per-cycle push or merge.** The run-level orchestrator (`internal/run/SKILL.md`) pushes once at the end of all cycles.
- **Abandonment is structured, not a hard abort.** When the cycle cannot complete, return a structured outcome dict with `status="abandoned"` and a `reason` code; the orchestrator records it and continues to the next cycle.

The clone already exists when this skill is invoked — `internal/clone-target/SKILL.md` set it up. The run branch is already checked out — `internal/run/SKILL.md` created it. This skill operates inside that environment.

## Execution

Cycles run in **subagent mode**: each Phase 2 participant is a separate fire-and-forget `Agent` tool call (one-shot dispatch, no shared state between agents); Phase 3 synthesis happens in the orchestrating agent's context. The mode does not change the cycle's logical structure (Phase 1–5).

The dispatch templates are exported from `scripts.dispatch_templates` — each is a pure function with explicit required-kwarg validation, corresponding 1:1 with the Phase 1-5c dispatch points.

## Loom comms — MANDATORY when available (F16)

When the Loom agent-chat server is available, inter-agent communication over loom-agent-chat is REQUIRED (CLAUDE.md rule F16; `KAIZEN_LOOM_COMMS=0` is the only opt-out). Loom failures must never block or abort a cycle — on any loom error, note it and continue.

**Scope.** These are the subagent-mode orchestrator's loom duties.

**At cycle start** the subagent-mode orchestrator runs (from the kaizen root):

```
PYTHONPATH=. python3 scripts/loom_comms.py detect
```

- `{"available": false, ...}` (exit 3) → note "loom: unavailable" once and proceed exactly as before. Skip the rest of this section.
- `{"available": true, "client": "<path>", ...}` (exit 0) → loom comms are mandatory for this cycle:

1. **Obtain the canonical channel name** (single naming authority — the exact name `scripts/loom_comms.py` derives; never compose one by hand):

   ```
   PYTHONPATH=. python3 scripts/loom_comms.py channel --run-id <run_id> --cycle <cycle_n>
   ```

   Then register and open it using the `client` path from the detect JSON: `python3 <client> register "team-lead"` — capture the returned `assigned_name` (it may be collision-suffixed, e.g. `team-lead-2`) and use it verbatim as `--as "<assigned>"` everywhere below — then `python3 <client> create-channel <chan> --as "<assigned>"` (or `join` if it already exists).
2. **Every dispatched Agent prompt MUST embed the loom block.** Obtain it via:

   ```
   PYTHONPATH=. python3 scripts/loom_comms.py block --role <role> --channel <chan>
   ```

   and append the printed block to the subagent's prompt. The block instructs the agent to register under its bare role id, join the channel, discover peers' ACTUAL assigned names from the channel member list before sending (registrations may be collision-suffixed), send peer communication via loom, check its inbox at phase boundaries, keep bodies ≤500 chars (file pointer under `.loom/temp/` in the working repo/clone root for longer content), and deregister on completion.
3. **Orchestrator reads the channel between phases** (`python3 <client> read <chan> --as "<assigned>"`, then `mark-read` what it processed) so cross-agent chatter informs synthesis/review decisions.
4. **Everyone deregisters at run end** — agents per their block; the orchestrator via `python3 <client> deregister --as "<assigned>"`.

Subagent completion signalling is unchanged by loom: the dispatched `Agent`'s returned final message remains the completion signal — loom carries cross-agent chatter, not completion.

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
  "reason": "no_consensus" | "destructive_rejected" | "tests_unrecoverable" | "review_unrecoverable" | "lint_failed" | "security_failed" | "sca_failed" | "bridge_timeout" | "other",
  "detail": "<free-text describing what was attempted and what blocked it>",
  "artifacts": ["<memex slug or path>", ...],
}
```

**Host path (`KAIZEN_TRANSPORT=host`).** The outcome dict comes straight from `scripts.host_cycle_entry` stdout — same success shape (`status/subject/commit_sha/minutes_memex_slug/participants`) and same abandoned shape. A `review_unrecoverable` abandonment from the host engine's review→fix loop additionally carries the four review-outcome keys (`review_iteration_count`, `unresolved_findings`, `convergence_summary`, `reviewer_attribution`), exactly as the prose path. **`peer_unconfirmed`** reviewer findings (blocker/major issues that survived without peer cross-confirmation — M8a-2c LOW-1) are surfaced **inside `convergence_summary`**: the host loop folds them into that text when it builds the abandonment. So when you render the abandonment report / cycle minutes (Phase 5d), the `convergence_summary` already names the peer-unconfirmed findings — surface it verbatim. On a clean success no findings survived, so there is nothing peer-unconfirmed to surface and the success dict stays the 5-key shape (no extra fields) — keep the PR-body / minutes render byte-identical to the prose success path.

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
   - **Code-nav graph (best-effort).** If a code-nav graph was built for this repo (Step 3.5 in `internal/run/SKILL.md`), PREFER it over grep + full-file reads for where-is / callers / dependencies / neighbors / module-map: run `PYTHONPATH=. python3 scripts/codegraph_recon.py where-is <repo> <symbol>` (and `callers` / `deps` / `neighbors` / `module-map`) from the kaizen root. It returns locations (file:line) as JSON, not file bodies — read a file only when you need its contents. If the graph was skipped (graphify/memex>=2.9.0 absent, or `KAIZEN_CODEGRAPH=0`), fall back to grep as usual.
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

#### Transport fork — `KAIZEN_TRANSPORT=host` is the DEFAULT (M8c)

**Check the transport FIRST.** When `KAIZEN_TRANSPORT` is unset, empty, or `host` (the DEFAULT as of M8c), Phase 4 (and the review→fix loop + commit) is delegated to atelier's in-process host engine via `scripts/host_cycle_entry.py` instead of being run by prose. The Phase 1-3 meeting stays exactly as-is — it produced the Action-Items DAG; the host path just hands that DAG to the engine.

Only when `KAIZEN_TRANSPORT=prose` is set EXPLICITLY (the opt-out from the default host engine) do you run the prose Phase-4 / 5a / 5b / 5b' / 5c procedure below VERBATIM. A typo'd value fails loud (`UnknownTransportError`) — do not guess.

**Host-path procedure:**

1. **Serialize the Action-Items DAG to JSON.** Write the kaizen-NATIVE DAG the meeting produced (one object per Action Item) to a gitignored file in the clone — use `.ai/host_action_items.json` (the `.ai/` directory is gitignored, so it never lands in the target's PR diff). Each item carries the kaizen-native keys ONLY:
   - Required kaizen-native keys: `id` (str), `touches` (list[str]), `reads` (list[str]), `depends_on` (list[str]), `wave` (int), plus optional `owner` (str).
   - **Extra meeting keys are fine.** The synthesis meeting also emits `description` (and may carry other keys) — pass the meeting's items through unchanged. `validate_dag` tolerates extra keys and `build_engine_tasks` ignores them; you do NOT have to strip anything.
   - Do **NOT** emit engine-OUTPUT keys (`task_id`, `parallel_group`, `writes`, `assigned_persona`, `phase`) — those are the OUTPUT of `build_engine_tasks`. The entry FAILS FAST (clean stderr line + exit 2, `ActionItemsShapeError`) if it sees them, naming the offending key, so you fix the serialization rather than getting an opaque error deeper in. (A native-looking-but-malformed DAG — e.g. a missing required key or a wrong-typed `touches` — also exits 2 cleanly.)

   **Worked example** (`.ai/host_action_items.json`):
   ```json
   [
     {
       "id": "AI-1",
       "description": "Add a guard to foo()",
       "touches": ["scripts/foo.py"],
       "reads": [],
       "depends_on": [],
       "wave": 1,
       "owner": "backend-engineer-1"
     },
     {
       "id": "AI-2",
       "description": "Cover foo()'s guard with a test",
       "touches": ["tests/test_foo.py"],
       "reads": ["scripts/foo.py"],
       "depends_on": ["AI-1"],
       "wave": 2,
       "owner": "sdet-1"
     }
   ]
   ```

2. **Invoke the host entry** from the kaizen root:
   ```
   PYTHONPATH=. KAIZEN_TRANSPORT=host python3 -m scripts.host_cycle_entry \
       --action-items-file <clone_dir>/.ai/host_action_items.json \
       --clone-dir <clone_dir> \
       --subject "<run_row['subject'] or omit>" \
       --roster <resolved role id 1> <resolved role id 2> ... \
       --pm <pm role id> \
       --cycle-n <cycle_n> [--run-id <run_id>] \
       --test-command '<project["test_command"]>'
   ```
   - `--roster` is the resolved participant role ids from Phase 1; `--pm` defaults to `roster[0]` if omitted.
   - **The roster MUST include at least one role NOT used as an Action-Item `owner`.** The host engine runs the Phase 5b' review with reviewers DISJOINT from the implementers (an agent cannot review its own work); if every roster role is an owner, no disjoint reviewer pool exists and the cycle abandons at the review phase (`reason="other"`, roster-too-small). Pass a roster that is strictly larger than the set of owners.
   - `--test-command` MUST mirror the target repo's CI test command (F2) — the host engine's post-Phase-4 CI gate runs it.

3. **The returned JSON IS the cycle outcome.** stdout carries the outcome dict (same shape as "Outcome (return)" below). The host engine runs the WHOLE remainder of the cycle internally:
   - Phase 4 implementation waves (engine-scheduled from `depends_on`),
   - the Phase 5b' independent-reviewer review→fix loop (Star→Mesh→Star), AND
   - the post-Phase-4 CI-mirror gate, AND
   - the cycle COMMIT (`commit_cycle_and_sha`, internal — F3 holds with no extra commit).

   On the host path you therefore **DO NOT** run prose Phase 5a/5b/5b'/5c and you **DO NOT** call `commit_cycle` by hand — doing so would double-review and double-commit. Read the outcome JSON, set the Memex slug for Phase 5d minutes capture (the slug is in `minutes_memex_slug`), then go straight to Phase 5d. A non-zero exit (a DAG-shape error from `ActionItemsShapeError` or `validate_dag`, or the transport guard) is an operator/serialization bug to fix, not a cycle abandonment — the entry prints a clear `host_cycle_entry: <msg>` line on stderr; fix the input and re-invoke.

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

### Phase 5d — Minutes (captured to Memex; not committed to git)

Cycle minutes are **process artifacts** per kaizen#51 and the `### Process-artifact storage` rule in `CLAUDE.md`. They MUST NOT be committed to the kaizen git tree. The canonical store is **Memex**; `docs/kaizen/` is gitignored.

The orchestrator may write the minutes to a temporary path under `docs/kaizen/<YYYY-MM-DD>-cycle-<n>-minutes.md` for the user to inspect locally (the directory is gitignored, so the file will not be staged), then capture them to Memex via the orchestrating Claude session — `memex:run` is a Claude Code skill, not a CLI binary, so the Python orchestrator cannot invoke it directly. The orchestrating session calls:

```
memex:run capture --id kaizen:cycle:<run_id>-<cycle_n> docs/kaizen/<YYYY-MM-DD>-cycle-<n>-minutes.md
```

Slug convention: `kaizen:cycle:<run_id>-<cycle_n>`. Once captured, the local file may be deleted; Memex is the durable store.

Set `minutes_memex_slug = "kaizen:cycle:<run_id>-<cycle_n>"` in the cycle return dict so the orchestrator surfaces it in the run summary — the slug is the intended identifier even though capture is manual.

### Return outcome

Return the success dict described under "Outcome (return)" with the slug, commit sha, and resolved participant names.

## Hard rules

- **In-cycle test fix iteration is mandatory.** Do not escalate a test failure to "next cycle" until the agents have actually attempted to repair it (typically up to 3 rounds).
- **Abandonment is structured.** Always return a dict with a `reason` from the nine named codes mirrored in `scripts/abandonment.py::VALID_REASONS` (`no_consensus`, `destructive_rejected`, `tests_unrecoverable`, `review_unrecoverable`, `lint_failed`, `security_failed`, `sca_failed`, `bridge_timeout`, `other`). The orchestrator depends on this for the abandonment report.
- **Working tree must be clean at cycle end** — committed (success path) or reset (abandon path). The next cycle inherits the same clone; an unclean tree leaks state.
- **Unanimous consensus is required** (synthesis meeting). Anything less drops the item; if every item drops, the cycle abandons.
- **Destructive changes require explicit user approval** — never bypass the destructive check, even when the user has pre-authorized other parts of the flow.
