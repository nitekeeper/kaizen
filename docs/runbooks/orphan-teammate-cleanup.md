# Orphan teammate cleanup

Troubleshooting runbook for orphan teammates left behind by Claude Code Agent Teams runs. Applies to any kaizen team-mode run; also useful for direct CC team-mode use.

## Symptoms

You may have orphan teammates if any of these are true:

- `claude` processes consume RAM long after a kaizen run finished
- The `tmux` window shows extra panes (e.g. a 9-pane window when the orchestrator only spawned one teammate)
- `~/.claude/teams/` contains directories for teams no live session owns
- `TeamDelete` calls in a fresh session no-op silently (per-session limitation — see `feedback-cc-teamdelete-per-session.md`)

## The trifecta

A spawned teammate is NOT one resource. It is **three resources at three layers**, each created and cleaned by a different primitive. `TeamDelete` only handles Layer 3.

| Layer | What it is | Lives in | Created by | Cleaned by | Visible via |
|---|---|---|---|---|---|
| **Process** | OS-level `claude --agent-id <name>@<team>` process consuming RAM | Linux process table | `Agent(team_name=...)` spawn | `shutdown_request` handshake → CC kills PID (or manual `kill <pid>`) | `pgrep -af '\-\-agent-id'` |
| **Pane** | tmux pane hosting the teammate's TTY/UI | Current tmux session/window | CC's spawn — one pane per teammate | Pane closes when the hosted process exits, usually (or `tmux kill-pane` — see Step 2 for the shell-stays-open edge case) | `tmux list-panes -a` |
| **Config** | Team manifest dir (roster, routing tables, team_id) | `~/.claude/teams/<team_id>/` (or equivalent) | `TeamCreate` | `TeamDelete` | `ls ~/.claude/teams/` |

**What `TeamDelete` does NOT do:**
- Does not send `shutdown_request` to active teammates
- Does not `kill` lingering Claude processes
- Does not `kill-pane` the tmux panes
- Only removes the team config dir

Calling `TeamDelete` alone with live teammates produces a **partial cleanup**: configs gone, processes and panes orphaned.

## Detection

Enumerate each layer independently. Run all three; cross-reference the results.

**Layer 1 — orphan processes:**
```bash
pgrep -af '\-\-agent-id'
```
Each line is a live Claude teammate process. Format: `PID command-with-args`. Note the `<name>@<team>` substring in `--agent-id` — useful for scoping `pkill`.

**Layer 2 — orphan panes:**
```bash
tmux list-panes -a -F '#{session_name}:#{window_index}.#{pane_index} pid=#{pane_pid} #{pane_current_command}'
```
Look for panes whose `pane_pid` matches a PID from Layer 1, or whose command is `claude`. A pane whose process has exited but whose shell stays open showing exit status also counts as orphan.

**Layer 3 — orphan configs:**
```bash
ls ~/.claude/teams/
```
Anything older than the most recent live session is suspect. Cross-check `team_id`s against the kaizen bridge DB. From the kaizen repo root, against `.ai/bridge.db`:

```bash
sqlite3 .ai/bridge.db "SELECT run_id, json_extract(response_json, '\$.team_id') FROM bridge_requests WHERE kind='team_create';"
```

…to see which kaizen runs created them.

## Recovery — post-GAP-7 (PRs ≥ #37)

**Should be automatic.** `team_cycle_executor`'s finally block fires `send_message_many(shutdown_request × N)` against every active teammate BEFORE `team_delete`. The handshake terminates each Claude process via CC's protocol layer; process exit closes the pane; `team_delete` removes the config. All three layers cleaned in order.

Smoke #4 (run 27, PR#38) empirically validated the happy path: 1 shutdown_request fired → architect terminated → no idle_notification → no orphan pane → `TeamDelete` clean.

**If you find orphans on a post-GAP-7 run, something is broken.** Likely causes:
- `send_message_many` raised before all members received `shutdown_request` (best-effort handshake — the warning `"GAP-7 shutdown send_message_many failed for team …"` should be in the log)
- A teammate failed to respond `{"approve": true}` (literal-minded teammates may misformat `request_id` — see `feedback-cc-team-mode-async-pattern.md` pitfalls)
- `active_members` was empty because the cycle aborted in Phase 1 before any `send_message` succeeded (expected — nothing to shut down)

Capture the bridge log and the run's leaked_teams.json before manual recovery; the leak is a regression worth filing.

## Recovery — pre-GAP-7 orphans

For orphans from runs before PR#37 (the GAP-7 fix), all three layers must be cleaned manually in order.

**Step 1 — kill processes (Layer 1):**
```bash
# Scoped — preferred. Anchored with `--` so the pattern matches only the
# CC argv flag, not arbitrary processes that happen to contain "agent-id"
# (e.g. a log tail). Replace prefix as appropriate (e.g. kaizen-cycle-).
pkill -f '\-\-agent-id .*@<team-name-prefix>'

# Or by PID, harvested from pgrep:
pgrep -af '\-\-agent-id' | awk '{print $1}' | xargs -r kill
```
SIGTERM is graceful enough; CC processes shut down cleanly on it.

**Step 2 — kill panes (Layer 2):**

Usually unnecessary — pane closes when its process exits. If panes linger (shell stays open showing exit status), derive the pane list from the Layer 1 PID list so you cannot kill unrelated panes:
```bash
ORPHAN_PIDS=$(pgrep -f '\-\-agent-id')
tmux list-panes -a -F '#{pane_id} #{pane_pid}' \
  | awk -v pids="$ORPHAN_PIDS" 'BEGIN{n=split(pids,a,"\n"); for(i=1;i<=n;i++) p[a[i]]=1} ($2 in p){print $1}' \
  | xargs -r -I{} tmux kill-pane -t {}
```
Do NOT filter panes by command name (e.g. `$2 == "bash"`) — that matches every shell on the machine, including your live orchestrator pane.

**Step 3 — clean configs (Layer 3):**

If the originating session is still alive, call `TeamDelete` from it (per-session contract). If not — the common case for cross-session orphans — `rm -rf` the directories directly:
```bash
rm -rf ~/.claude/teams/<team_id>/
```
`TeamDelete` invoked from a fresh session will no-op silently on cross-session orphans (see `feedback-cc-teamdelete-per-session.md`); filesystem removal is the only working path until a `TeamAttach`/`TeamLoad` primitive exists.

## Prevention

Use the GAP-7 `shutdown_request` handshake on every cleanup path. The kaizen bridge wires this in `team_cycle_executor`'s finally block via `tools.send_message_many` immediately before `tools.team_delete`. See `docs/design/python-cc-tool-bridge-design.md` section "Leaked-team recovery (Rev 3, GAP-7 addendum 2026-05-24)" for the protocol and best-effort failure semantics.

If you are building a non-kaizen orchestrator on top of CC Agent Teams, replicate the same pattern: structured JSON `{"type":"shutdown_request","request_id":"<uuid4>"}` per active member via `SendMessage`, then `TeamDelete`. Include the SHUTDOWN_BEHAVIOR clause in every teammate's spawn prompt so they know how to parse and respond.

## Empirical history

- **Smokes #2 (run 22) and #3 (run 24):** pre-GAP-7. Each `TeamDelete` left orphans behind. Cumulative: **8 orphan processes + 8 orphan panes** across the two runs, discovered 2026-05-24 post-arc. Configs were cleaned by `TeamDelete` as designed.
- **Smoke #4 (run 27, [PR#38](https://github.com/nitekeeper/kaizen/pull/38)):** post-GAP-7. Happy path validated end-to-end — `shutdown_request` fired, teammate terminated, pane auto-closed, `TeamDelete` clean. Zero leak in any layer.
