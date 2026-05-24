# Kaizen run 21 — cycle 1 minutes (2026-05-24)

**Subject:** Fix GAP-1 / GAP-2 / GAP-3 from run 20 smoke
**Mode:** subagent (bridge being fixed cannot reliably implement itself)
**Base branch:** main
**Branch:** kaizen/fix-gap-1-gap-2-gap-3-from-run-20-smoke-2026-05-24-0125
**Outcome:** SUCCESS (1 fix-loop iteration to converge)

## Phase 1 — PM agenda (abbreviated)
Subject names 3 specific bugs with concrete remediation hints from the run-20 smoke report. Phases 1-3 collapsed; the smoke report IS the synthesis output.

## Phase 2-3 — Participants
- **backend-engineer-1** (Implementer)
- **agent-systems-architect-1** (Independent reviewer)
- **ai-safety-researcher-1** (Independent reviewer)
- **prompt-engineer-1** (Independent reviewer)
- **PM (Dr. Priya Nair)** (Cycle lead)

## Phase 4 — Implementation (single pass)
Implementer applied all 3 fixes per spec:
- **GAP-1:** bumped `HEARTBEAT_STALL_S` 60→300, `PER_CALL_TIMEOUT_S` 180→600 in `scripts/cc_tool_bridge.py`. Trade-off documented inline.
- **GAP-2:** new module-level constant `TEAMMATE_REPLY_RULE` in `scripts/dispatch_templates.py`, appended to every teammate-bound template (10 templates, `phase_5b_ci_failure` excluded with negative test).
- **GAP-3:** SKILL Step 3b.3 spawn-line rewritten to use `export PYTHONPATH=.` and `export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` inside the `( umask 077 && ... )` subshell. Inline rationale + back-link to smoke report.

Plus housekeeping: design doc constant references synced; smoke report "Fixes applied" section added.

CI green from first pass: ruff check + ruff format --check + pytest (524 passed, 1 skipped, +4 net new test functions over the run-19 baseline).

## Phase 5a — Destructive check
Empty list, exit 0. Clean pass (no false positive this time, unlike runs 16-17 and 19).

## Phase 5b — Tests
Already green from Phase 4 (implementer self-verified).

## Phase 5b' — Independent reviewers + fix loop

### Round 1 verdicts
- **Architect:** APPROVED (2 MINORs — per-cycle outer deadline as follow-up; dead `time.monotonic` monkeypatch in one test)
- **Safety:** APPROVED WITH FIXES (0 BLOCKERs, 0 MAJORs, 2 MINORs — smoke-report Q markers; SKILL Step 2 inline-OK comment)
- **Prompt:** APPROVED WITH FIXES (**2 MAJORs** in `TEAMMATE_REPLY_RULE` wording)

The 2 MAJORs from prompt-engineer were the only finding that could partially reproduce GAP-2 in real practice:
- **MAJOR-1:** ambiguous "team-lead" recipient — needed literal copy-pasteable `to="team-lead"` example.
- **MAJOR-2:** ABANDON-vs-reply collision — does abandon teammate SendMessage or skip?

### Fix-loop iteration 1
Implementer applied both MAJORs + both safety MINORs. Architect's 2 MINORs deferred with `TODO(follow-up)` and `TODO(cosmetic)` comments in code.

- `TEAMMATE_REPLY_RULE` now includes `SendMessage(to="team-lead", message=<your reply>)` as literal copy-pasteable example + pinning sentence + explicit ABANDON-via-SendMessage clause.
- Test `test_every_template_appends_teammate_reply_rule` extended with two new assertions: `'to="team-lead"' in msg` AND `"ABANDON" in msg and "SendMessage" in msg`.
- Smoke report Q2/Q4 marked `→ RESOLVED in run 21`.
- SKILL Step 2 has inline-vs-export contrast note warning about Step 3 / GAP-3 trap.
- `TODO(follow-up): per-cycle outer deadline` at `scripts/cc_tool_bridge.py:240`.
- `TODO(cosmetic): unused monkeypatch` at `tests/test_cc_tool_bridge.py:264`.

### Round 2 verdict
Prompt-engineer re-audit: **APPROVED. "Ship."** Both MAJORs FIXED with copy-pasteable literals + test grep enforcement. 1 INFORMATIONAL — the `to="team-lead"` API assumption should be empirically confirmed in run 22 smoke (the next dogfood). Not a blocker.

Architect + Safety already approved in round 1; not re-spawned because the round-1 fixes were surgical to wording (prompt-engineer's lens) and neither raised any code-architecture concerns about the rule text itself.

**Final state:** 524 tests passing, 1 skipped, ruff check + format clean.

## Decisions log
1. **GAP-1: option (a) chosen** — bump constants over watchdog mechanism. Acceptable trade-off for personal-use single-machine deployment (5-min crash detection window, documented inline).
2. **GAP-2: option A chosen** — Python-side constant in `dispatch_templates.py` appended to every template. Most reliable (impossible for orchestrator to forget). Includes `to="team-lead"` literal AND ABANDON-via-SendMessage clause.
3. **GAP-3: explicit `export` inside subshell** — both `PYTHONPATH` and `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`.
4. **Two follow-up items DEFERRED:** per-cycle outer deadline (CYCLE_WALL_S=3600); dead test monkeypatch cleanup. Both have TODO comments in code.
5. **`to="team-lead"` API contract is theoretical** — confirmed by `TeamCreate`'s `lead_agent_id` output structure but not yet empirically validated by a teammate's reply reaching us via that exact `to=` value. Run 22 smoke is the validator.

## Files added / modified
**Modified (8):** `scripts/cc_tool_bridge.py`, `scripts/dispatch_templates.py`, `skills/improve/SKILL.md`, `docs/design/python-cc-tool-bridge-design.md`, `docs/kaizen/2026-05-24-bridge-smoke.md`, `tests/test_cc_tool_bridge.py`, `tests/test_dispatch_templates.py`, `tests/test_dispatch_templates_byte_identity.py`.

**No new files** — all 3 GAPs were surgical edits to existing files.

## CI status at commit
- `ruff check .` — PASS
- `ruff format --check .` — PASS (67 files)
- `pytest -v --tb=short` — 524 passed, 1 skipped, ~15s

## Next step after this PR merges
Run 22 smoke (`/kaizen:improve --mode team`) — second dogfood. Empirically validates:
- GAP-1 fix (longer SendMessage waits no longer trip BridgeStallError)
- GAP-2 fix (teammates actually SendMessage back instead of going idle)
- GAP-3 fix (export PYTHONPATH propagates correctly)
- `to="team-lead"` API contract (the round-2 INFORMATIONAL)
- Open Q #1 (team_id cross-session scoping — still unresolved from run 20)

Then if all green, team-mode is genuinely production-ready for personal use.
