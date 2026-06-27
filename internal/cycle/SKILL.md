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

The rest of Phase 4 (prose form) and Phases 5a / 5b / 5b-prime / 5c are the explicit `KAIZEN_TRANSPORT=prose` opt-out path. They are NOT the default and run ZERO lines on a default cycle.

When `KAIZEN_TRANSPORT=prose` is set EXPLICITLY, the orchestrator MUST `Read` `internal/cycle/prose-transport.md` and follow its Phase 4 (prose) / 5a / 5b / 5b-prime / 5c procedure VERBATIM, then return here for Phase 5d.

On the DEFAULT host path (`KAIZEN_TRANSPORT` unset, empty, or `host`), none of that applies — the host engine (`scripts.host_cycle_entry`) subsumes Phases 4-5c per the transport fork above, so do NOT read `prose-transport.md`; go straight from the host-path procedure to Phase 5d below.

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
