# Bridge smoke run — 2026-05-24 (run 20)

First real `/kaizen:improve --mode team` dogfood. Subject: trivial single-file docstring addition for `scripts/bridge_db.py`. **Outcome: ABANDONED at SendMessage #1**, but the abandonment was structurally clean and the bridge mechanics worked as designed for every round-trip that did occur.

## What the smoke validated (✅)

1. **`scripts/bridge_db.py::bootstrap()` works.** Three tables created (`bridge_requests`, `bridge_heartbeat`, `python_heartbeat`), `PRAGMA journal_mode=WAL`, `busy_timeout=5000` all set. Idempotent.
2. **`create-run-only` CLI works.** Printed run_id=20 cleanly on stdout, no warning noise. Branch sentinel `<pending>` written correctly to the run row.
3. **mfix-UMASK works empirically.** `( umask 077 && nohup ... > /tmp/kaizen-bridged-20.log ... & )` → file mode `0600` (verified `ls -la`). The subshell-umask trick succeeds; without it the file would be 0644 (parent shell default).
4. **`run_bridged.py` env validation works.** First spawn attempt failed loudly with `missing required env vars: PYTHONPATH` and Python exited without side effects. No clone/seed/branch happened on the abort.
5. **TeamCreate round-trip via the bridge.** `args_json` → `TeamCreate(team_name=...)` → `team_id` written back via `bridge_write.py` → Python observed `status='ready'` and proceeded.
6. **SendMessage write-back path works.** Architect's multi-line agenda response (771 bytes incl. unicode arrows ↔ and em-dashes —) JSON-encoded via `json.dumps`, piped to `bridge_write.py --status ready`. SQLite parameter binding preserved the payload verbatim.
7. **TeamDelete cleanup (post-abandonment) works.** Python's `finally` block enqueued the TeamDelete row; S1 serviced it; the team config + task list directories were removed cleanly by the `TeamDelete` tool.
8. **`update_run_branch(None) → '<failed>'` sentinel works.** Run row has `branch='<failed>'` after Python's BridgeStallError exception — exactly as MAJOR-NEW-BRANCH-NOT-NULL was designed.
9. **`render_pr_body` refusal-set protected the PR-open path.** The orchestrator correctly skipped PR open when `branch='<failed>'` — verified via design Step 3b.5's documented behavior.
10. **`bridge_write.py` parameter binding handled multi-line + unicode + JSON in stdin.** Zero injection attempts (the input was honest), but the empirical round-trip of a 771-byte payload containing `\n`, `↔`, `—`, `"`, parentheses, etc. confirms the `?`-binding pipeline works for non-trivial real-world strings.

## What the smoke uncovered (⚠️ design gaps)

### GAP-1 (MAJOR) — `HEARTBEAT_STALL_S=60s` is fundamentally too short for CC team-mode

**Symptom.** Python raised `BridgeStallError: S1 heartbeat stalled: last_polled_at is 60.2s old` after my SendMessage→architect→reply round-trip took ~50 seconds.

**Root cause.** The Rev 4 design assumes the per-row heartbeat poke (Appendix A step 2a — fires BEFORE every session-tool dispatch) bounds the heartbeat gap to one Bash latency (~1-3s). This is true for `TeamCreate` and `TeamDelete` (synchronous session tools that return immediately). It is **false for `SendMessage` in CC team mode**, which is fundamentally async: I call `Agent(team_name=..., ...)` or `SendMessage(to=..., ...)` and the teammate's response arrives later via an INCOMING `<teammate-message>` notification. During the wait for the incoming message, the orchestrating Claude (S1) is idle — no tool calls fire, no heartbeats are poked, and the 60s threshold elapses.

**The reviewer's Open Q #4 (round-3 review of PR#29) predicted this.** Quoting: *"`HEARTBEAT_STALL_S=30s` is still likely to spuriously trip on long SendMessage calls."* The architect bumped to 60s in Rev 4, which is still insufficient. **Empirical data point: real teammate replies take 30-60+s in this single-cycle smoke; multi-turn agent meetings would extend further.**

**Remediation options** (carry into a follow-up cycle):
- **(a) Bump `HEARTBEAT_STALL_S` to 300s+.** Trade-off: a real Python crash would be undetected for 5+ minutes. Acceptable if the per-call timeout (`PER_CALL_TIMEOUT_S=180s`) is also bumped.
- **(b) Have S1 poke heartbeat while waiting for incoming teammate messages.** Requires interleaving Bash calls between async waits — feasible because the orchestrator's tool-use loop continues to fire (e.g., reading the bridge db, checking other rows). Probably the right answer.
- **(c) Rearchitect: have Python's stall check measure SOMETHING ELSE.** E.g., presence of a heartbeat-poking S1 process via OS signals. Too invasive for the personal-use scope.

**Recommendation:** Option (b). The orchestrator already pokes heartbeat in step 1 of every poll iteration. The fix is to ensure step 1 (combined SQL + heartbeat UPSERT) re-fires while waiting for a long teammate reply, not just after the reply arrives.

### GAP-2 (MAJOR) — `team_executor.py`'s `send_message` wire-protocol assumes synchronous round-trip; CC team mode is fundamentally async

**Symptom.** When I spawned the architect via `Agent(team_name='kaizen-cycle-20-1', name='agent-systems-architect-1', prompt=<Phase 1 brief>)`, the agent processed the spawn-prompt and went idle WITHOUT delivering its response back to me. I had to send a follow-up `SendMessage` asking explicitly: "please reply with your agenda items." The architect then complied via an explicit `SendMessage` back to me.

**Root cause.** In CC team mode the Agent tool's spawn-prompt output is NOT auto-relayed to the team-lead. Recipients must explicitly invoke `SendMessage` to send their response. The Python team_executor's wire protocol implicitly assumes: `wrapper.send_message(team_id, to, message) → response`. The "response" is supposed to flow back automatically.

**Remediation options:**
- **(a) Append a hard rule to every spawned teammate's prompt:** "After completing your task, send your response back to team-lead via SendMessage. Do NOT just go idle." Cheap and effective.
- **(b) Restructure the team_executor wire to model async replies explicitly.** E.g., `send_message` returns a `message_id`; Python polls a `bridge_replies` table for the matching `message_id`. More invasive.
- **(c) Use `Agent` tool's foreground mode (no `team_name`, no `run_in_background`) for the Phase 1-2 brief-and-response pattern.** Treats each teammate-turn as a synchronous Agent call instead of a long-lived team member. Simpler but loses the persistent-team benefit.

**Recommendation:** Start with (a) — least invasive, captures 80% of the value. Revisit (c) if Phase 5b' reviewer rounds also suffer from the async-reply pattern.

### GAP-3 (MINOR) — CC Bash inline env vars don't propagate to nohup-detached subshell

**Symptom.** First spawn attempt at Step 3b.3 used `PYTHONPATH=. nohup python3 ...` and Python's env-validation rejected with `missing required env vars: PYTHONPATH`.

**Root cause.** Bash inline assignment `VAR=val command` only exports the var to the immediate command's environment. When wrapped in `( umask 077 && nohup ... & )` the subshell starts a fresh job and the inline VAR isn't inherited.

**Remediation:** SKILL Step 3b.3 should use `export PYTHONPATH=.` explicitly before the `( ... )` subshell, OR add `PYTHONPATH=. ` as a prefix INSIDE the subshell (still works since it precedes the `nohup` directly). Document in the SKILL.

**Empirical answer to design's Open Q #2 (CC Bash-tool env inheritance):** The Bash tool's subshell inherits exported parent-shell vars; it does NOT inherit one-off inline assignments to a `( ... & )` subshell. `run_bridged.py`'s belt-and-braces env-validation caught this cleanly — without it, the cycle would have produced a confusing ImportError mid-run.

## Open question status

- **Q1 (Anthropic-side team_id cross-session scoping):** **Not yet empirically answered.** The cycle abandoned before producing a leaked team_id we could attempt to delete from a different session. Carry to next smoke.
- **Q2 (CC Bash env precedence):** **EMPIRICALLY ANSWERED** — see GAP-3 above. → **RESOLVED in run 21** (see "Fixes applied 2026-05-24 (run 21)" below).
- **Q4 (heartbeat threshold):** **EMPIRICALLY ANSWERED** — 60s is too short; see GAP-1. → **RESOLVED in run 21** (see "Fixes applied 2026-05-24 (run 21)" below).

## Bridge DB final state (post-cleanup)

```
bridge_requests rows for run 20:
  id=1  kind=team_create   status=ready  response_json={"team_id":"kaizen-cycle-20-1"}
  id=2  kind=send_message  status=ready  response_json={"response":"<5-line agenda>"} (771 bytes)
  id=3  kind=team_delete   status=ready  response_json={}

bridge_heartbeat: run_id=20, polled_count=7, last_polled_at=2026-05-24 00:48:43

python_heartbeat: run_id=20, polled_count=788, last_beat_at=2026-05-24 00:47:16
```

Python heartbeated 788 times during the ~50s wait for the architect's response (4 polls/sec × 50s = 200 ticks per send_message × ~3-4 send_messages worth before stall). S1 (me) heartbeated 7 times — once per major tool-call boundary, which is exactly the gap GAP-1 describes.

## Run row final state

```
run_id=20
status=failed
branch=<failed>      ← sentinel from update_run_branch(None) — MAJOR-NEW-BRANCH-NOT-NULL fix working
cycles_succeeded=0
cycles_abandoned=0   ← Python crashed via exception; didn't structurally abandon via skip-and-continue path
pr_url=null          ← PR open skipped per Step 3b.5's render_pr_body refusal of <failed> branch
ended_at=2026-05-24T00:47:16Z
```

## Verdict

**Bridge mechanics: verified ✅.** Every plumbing assumption that COULD be exercised in this short smoke worked correctly. The injection-protected write path, the parameter binding, the umask-protected log file, the env validation, the `<failed>` sentinel propagation, the refusal-guard on PR open — all functioned exactly as PR#29 + PR#30 specified.

**Bridge semantics: 2 MAJOR gaps surfaced that block a real multi-turn cycle.** GAP-1 (heartbeat-stall during async teammate-reply wait) and GAP-2 (teammates don't auto-relay spawn-prompt output) are both fixable but require a follow-up design + implementation cycle. Neither was anticipated correctly by the design's 4 review rounds — they're genuinely new findings from real-world exercise. This is exactly what a smoke is for.

**Recommended next cycle subject:** *"Fix HEARTBEAT_STALL_S handling for async CC team-mode SendMessage (option (b): orchestrator pokes heartbeat while waiting for incoming teammate messages), AND add 'always SendMessage back to team-lead' hard rule to every teammate's spawn prompt (or to SKILL prose telling S1 to inject it). NON-DESTRUCTIVE. Validates against a second smoke run."*

## Fixes applied 2026-05-24 (run 21)

All three GAPs were addressed in this cycle. NON-DESTRUCTIVE — no public-function deletions, no schema migrations, no test deletions (only updates to existing tests + new tests added).

### GAP-1 — Fixed in run 21 via option (a) — constant bumps; trade-off acceptable for personal use.

- `scripts/cc_tool_bridge.py`: `HEARTBEAT_STALL_S` 60 → 300, `PER_CALL_TIMEOUT_S` 180 → 600. Inline comment documents the trade-off (5-minute crash-invisibility window in exchange for elimination of spurious abandonment on legitimate CC team-mode async-SendMessage round-trips).
- Tests:
  - `tests/test_cc_tool_bridge.py::test_bridge_stall_raises_when_s1_heartbeat_old` — updated offset 120 → 600 so the test still trips the stall under the new threshold.
  - `tests/test_cc_tool_bridge.py::test_heartbeat_stall_constants_match_run21_values` — NEW; pins both constants to 300/600 so a future drift back to 60/180 fails loudly.
  - `tests/test_cc_tool_bridge.py::test_long_sendmessage_does_not_trip_stall_at_old_threshold` — NEW; simulates a 90s SendMessage with NO interleaved S1 heartbeat pokes (the exact run-20 failure mode) and asserts NO BridgeStallError under the new 300s threshold.
- `docs/design/python-cc-tool-bridge-design.md`: code-sketch + prose references to `HEARTBEAT_STALL_S=60s` / `PER_CALL_TIMEOUT_S=180s` updated to `HEARTBEAT_STALL_S=300s` / `PER_CALL_TIMEOUT_S=600s` so design + code stay in sync.

### GAP-2 — Fixed in run 21 via option (a) hard-rule append to every teammate-dispatch template.

- `scripts/dispatch_templates.py`: new module-level constant `TEAMMATE_REPLY_RULE` appended to the return value of every teammate-bound template (`phase_1_agenda`, `phase_2_preanalysis`, `phase_3_open`, `phase_3_debate`, `phase_3_close`, `phase_4_implementer`, `phase_5b_prime_reviewer`, `phase_5b_prime_fix`, `phase_5b_prime_pm_acceptance`). The rule instructs the recipient to explicitly `SendMessage` its response back to team-lead — silent spawn-prompt completion no longer kills the cycle. `phase_5b_ci_failure` is EXCLUDED on purpose: it formats an abandonment-outcome detail string, not a teammate message body.
- Tests:
  - `tests/test_dispatch_templates.py::test_every_template_appends_teammate_reply_rule` — NEW parametrized test iterating every teammate-dispatch template with its minimum-valid kwargs and asserting (1) the rule appears in the body and (2) the body ENDS with the rule (so it is appended, not prepended).
  - `tests/test_dispatch_templates.py::test_phase_5b_ci_failure_does_NOT_append_teammate_reply_rule` — NEW boundary test: pins the abandonment-detail formatter as the explicit exception.
  - `tests/test_dispatch_templates_byte_identity.py` — every teammate-dispatch template's golden updated to include the rule suffix (`+ _RULE`); `phase_5b_ci_failure_golden` left untouched.

### GAP-3 — Fixed in run 21 by switching to `export` inside the subshell.

- `skills/improve/SKILL.md` Step 3b.3: spawn-line now uses `export PYTHONPATH=.` and `export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` inside the `( umask 077 && ... )` subshell rather than Bash inline `VAR=val command` prefixes that don't propagate to nohup-detached subshells. Inline prose documents the empirical run-20 rationale and back-links to this smoke report's GAP-3 section.
- Tests: none — this is a SKILL prose fix. A regression smoke (run 21+) will validate the spawn-line empirically.
