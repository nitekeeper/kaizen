# Kaizen Run 12 Cycle 1 Minutes — kaizen

**Date:** 2026-05-23 UTC
**Run ID:** 12
**Facilitator:** Dr. Priya Nair (PM)
**Subject:** Add team agent mode alongside existing subagent mode
**Participants:**
| Agent | Role |
|---|---|
| Dr. Nadia Petrov | Agent Systems Architect |
| Dr. Fatima Al-Rashid | AI Safety Researcher |
| Dr. Yusuf Okafor | Prompt Engineer |
| Dr. Aisha Mensah | Cognitive Scientist |
| Dr. Samuel Okafor | Software Engineer (Backend) |
| Dr. Aisha Kamara | Data Engineer |

---

## Discussion

### Agenda Item 1: Where should the --mode flag be introduced?

**Proposals:**
- Dr. Nadia Petrov (Agent Systems Architect): The `cycle_executor` injection point in `orchestrate_run` is already the right abstraction. Add `--mode subagent|team` to the skill invocation signature and thread it as `mode=` parameter to `orchestrate_run`. No orchestrator changes beyond a new parameter.
- Dr. Samuel Okafor (Backend Engineer): Agreed. Mode as a runtime parameter, not a DB column. Select executor inside `orchestrate_run` based on `mode` value when no `cycle_executor` is explicitly injected.
- Dr. Yusuf Okafor (Prompt Engineer): `skills/improve/SKILL.md` must document the new flag clearly — invocation signature, Step 2 parse/validate, and Step 3 route.

**Discussion:** All agents agreed immediately. No objections. The existing executor injection pattern is the right seam.

**Decision:** Add `mode: str = 'subagent'` to `orchestrate_run` signature. Select `team_cycle_executor` when `mode='team'` and no `cycle_executor` is injected. Update `skills/improve/SKILL.md` invocation section and Steps 2–3. — *Unanimous*

### Agenda Item 2: Python implementation of team_executor.py

**Proposals:**
- Dr. Nadia Petrov (Agent Systems Architect): Standalone `scripts/team_executor.py` with `team_cycle_executor(clone_dir, project, run_row, cycle_n)` — matches `execute_cycle` signature exactly.
- Dr. Fatima Al-Rashid (AI Safety Researcher): Must include `_check_team_tools_available()` guard that raises `TeamToolsUnavailableError` when `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` env var is absent or falsy. Fail fast with a human-readable message rather than an obscure ToolNotFound error deep in the cycle.
- Dr. Aisha Mensah (Cognitive Scientist): Standalone module is the right cognitive design — engineers understand two clear modules with identical interfaces better than a single module with mode-branching.
- Dr. Samuel Okafor (Backend Engineer): The function body raises `NotImplementedError` because real execution requires live Agent Teams tool calls in a Claude Code session — a Python subprocess cannot call those tools. This mirrors the `execute_cycle` stub design.

**Discussion:** All agents aligned. The safety guard was unanimously accepted as a mandatory pre-flight check.

**Decision:** Create `scripts/team_executor.py` with `TeamToolsUnavailableError` class, `_check_team_tools_available()` guard, and `team_cycle_executor()` that raises `NotImplementedError` after passing the guard. — *Unanimous*

### Agenda Item 3: Project-level field vs CLI flag

**Proposals:**
- Dr. Aisha Kamara (Data Engineer): Mode is a runtime concern, not a storage concern. No migration needed. Accepted trade-off: past runs don't record which mode they used. Document as known gap in `internal/cycle/SKILL.md`.
- All other agents: Agreed. Adding a schema migration for a feature flag adds complexity with low payoff at this stage.

**Decision:** No migration. Mode is a parameter only, not stored in the `runs` table. Document the known gap in `internal/cycle/SKILL.md`. — *Unanimous*

### Agenda Item 4: Tests

**Proposals:**
- Dr. Samuel Okafor (Backend Engineer): `tests/test_team_executor.py` — unit tests for the guard function (truthy/falsy/absent env var), executor signature match, and error message content. `tests/test_run.py` — two new tests: (1) `mode` key appears in result dict, (2) `mode='team'` without injected executor selects `team_cycle_executor` (verified via `TeamToolsUnavailableError` propagation and run finalization at `status='failed'`).
- Dr. Fatima Al-Rashid (AI Safety Researcher): Signature match test is important — if `team_cycle_executor` diverges from `execute_cycle`, the orchestrator's swapping logic breaks silently.

**Decision:** Add `tests/test_team_executor.py` (16 tests) and two new tests to `tests/test_run.py`. — *Unanimous*

### Agenda Item 5: SKILL.md prose update

**Proposals:**
- Dr. Yusuf Okafor (Prompt Engineer): `internal/cycle/SKILL.md` gets a new "Execution modes" section (table + per-mode dispatch description + env var requirement + known gap on mode persistence). `skills/improve/SKILL.md` invocation updated with `--mode` flag, Step 2 validation extended, Step 3 route updated to pass `mode`.
- All agents: Agreed.

**Decision:** Update `internal/cycle/SKILL.md` with "Execution modes" section. Update `skills/improve/SKILL.md` invocation + Steps 2–3. — *Unanimous*

---

## Decisions Log

1. Add `mode: str = 'subagent'` parameter to `orchestrate_run` in `scripts/run.py`; select `team_cycle_executor` when `mode='team'` — `scripts/run.py`
2. Create `scripts/team_executor.py` with `TeamToolsUnavailableError`, `_check_team_tools_available()`, and `team_cycle_executor()` — `scripts/team_executor.py`
3. No DB migration; mode is runtime-only; known gap documented in `internal/cycle/SKILL.md` — `internal/cycle/SKILL.md`
4. Add `tests/test_team_executor.py` (16 tests) + 2 new tests in `tests/test_run.py` — `tests/test_team_executor.py`, `tests/test_run.py`
5. Update `skills/improve/SKILL.md` invocation + Steps 2–3; update `internal/cycle/SKILL.md` with "Execution modes" section — `skills/improve/SKILL.md`, `internal/cycle/SKILL.md`

## Action Items

| # | Change | File(s) | Assigned to |
|---|---|---|---|
| 1 | Add `mode` param to `orchestrate_run`, select executor by mode, include `mode` in result dict | `scripts/run.py` | Dr. Samuel Okafor |
| 2 | Create team executor module with safety guard and `NotImplementedError` stub | `scripts/team_executor.py` | Dr. Fatima Al-Rashid |
| 3 | Document "Execution modes" section + known persistence gap | `internal/cycle/SKILL.md` | Dr. Aisha Kamara |
| 4 | Write test suite for team executor + orchestrator mode selection | `tests/test_team_executor.py`, `tests/test_run.py` | Dr. Samuel Okafor |
| 5 | Update SKILL.md prose (invocation + Steps 2–3 + mode routing) | `skills/improve/SKILL.md`, `internal/cycle/SKILL.md` | Dr. Yusuf Okafor |

---

*All action items implemented in this cycle. Tests: 249 passed, 1 skipped (18 new tests added).*
