# Loom comms — F16 mandatory procedure

The F16 mandatory loom-agent-chat procedure, read ONLY when `internal/cycle/SKILL.md`'s detect step returns `available: true` (exit 0). When detect returns `available: false`, do NOT read this file.

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
