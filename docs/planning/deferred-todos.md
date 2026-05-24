# Deferred TODOs — kaizen plugin

<!--
Issue numbers in the Summary table and per-item links were filed against
the kaizen GitHub repo on 2026-05-24 (issues #40–#45). Filing log:
docs/planning/issues/issue-{1..6}-*.md were the body sources.
-->

Tracking doc for non-urgent follow-up work captured during the bridge arc
(PRs #28–#39, 2026-05-23 → 2026-05-24). Each item has a corresponding
GitHub issue; **none are blocking** — fix as bandwidth allows.

All file:line references current as of HEAD (`main @ 3a1251b`, 2026-05-24).
Verify before acting on a stale entry — line numbers drift with edits.

## Summary

| # | Title | Priority | Effort | Issue |
|---|-------|----------|--------|-------|
| 1 | Cross-run bridge DB row purge | Low | M | [#40](https://github.com/nitekeeper/kaizen/issues/40) |
| 2 | Monotonic-friendly heartbeat stall check (laptop-suspend skew) | Medium | M | [#41](https://github.com/nitekeeper/kaizen/issues/41) |
| 3 | Per-cycle outer wall-clock deadline (`CYCLE_WALL_S`) | Low | S | [#42](https://github.com/nitekeeper/kaizen/issues/42) |
| 4 | Remove dead `time.monotonic` monkeypatch in bridge test | Low | XS | [#43](https://github.com/nitekeeper/kaizen/issues/43) |
| 5 | Add Bandit + pip-audit to in-clone CI mirror | Low | M | [#44](https://github.com/nitekeeper/kaizen/issues/44) |
| 6 | Promote orphan-cleanup recipe into `scripts/cleanup_orphans.py` | Low | M | [#45](https://github.com/nitekeeper/kaizen/issues/45) |

---

## Item 1 — Cross-run bridge DB row purge

**File:** `scripts/bridge_db.py:84` (TODO comment in `bootstrap()` docstring)
**Origin:** Review round 1 of GAP-4 batch wrapper PR (PR#36, marker `m3`).
**Description:** `bootstrap()` is idempotent w.r.t. schema but never DELETEs rows. `bridge_requests`, `bridge_heartbeat`, and `python_heartbeat` accumulate across many runs. With GAP-4 batching each cycle writes ~10× the rows it used to (empirical row-count delta observed in `bridge_requests` between runs 22 and 24), so growth is faster than pre-arc estimates assumed.
**Why deferred:** Cleanup semantics need design discussion (purge on `bootstrap()`? on `finalize_run()`? by age? by `run_id`?). `STALE_ROW_S` handles per-run stale detection but no cross-run cleanup. DB is local-only, growth is bounded by user runs, no functional impact yet.
**Suggested approach:** Pick a purge trigger (bootstrap-time age sweep is simplest), add a `purge_old_rows(cutoff_age_s)` helper, decide retention window (suggest 7 days), document in `scripts/bridge_db.py` module docstring.

## Item 2 — Monotonic-friendly heartbeat stall check (laptop-suspend skew)

**File:** `scripts/cc_tool_bridge.py:407` (was `:194` in memory — line drifted; the TODO comment lives in `_s1_seconds_since_last_poll()` docstring)
**Origin:** Review round 1 (marker `m4`), DEFER decision.
**Description:** `_s1_seconds_since_last_poll()` uses SQLite `julianday('now')` minus `bridge_heartbeat.last_polled_at` to detect S1 stalls. On laptop suspend/resume, wall-clock can jump 8h forward while Python's `time.monotonic()` stays still — a spurious resumed-into-stale state would trip `BridgeStallError` and abandon the cycle.
**Why deferred (and why Medium not Low):** Current `HEARTBEAT_STALL_S=300` mitigates short suspends but not overnight closes. Kaizen's primary deployment surface is a laptop the user closes between sessions, so this failure mode is realistic and recurring; reviewer re-classified Low → Medium accordingly. Cross-platform monotonic-friendly stall check needs design (SQLite doesn't expose monotonic time; would need a Python-side cross-check or a wake-detection signal).
**Suggested approach:** Hybrid check — raise `BridgeStallError` ONLY when both (`julianday('now')` gap > threshold) AND (monotonic gap since last `_tick_python_heartbeat()` > threshold). If julianday gap is large but monotonic gap is small, treat as suspect (likely suspend-resume), reset the deadline, and continue.

## Item 3 — Per-cycle outer wall-clock deadline (`CYCLE_WALL_S`)

**File:** `scripts/cc_tool_bridge.py:438` (was `:240` in memory — line drifted; the TODO lives in `_request()` body)
**Origin:** Review round 1, architect MINOR finding from PR#34 review.
**Description:** Worst-case wall-clock is `PER_CALL_TIMEOUT_S × dispatches_per_cycle`. A 50-call cycle could in principle block 8h+ before any single-call timeout fires. A per-cycle `CYCLE_WALL_S` (~3600s) cap would bound this.
**Why deferred:** Empirically, observed cycle wall-clock has never approached the worst case; the per-call timeout has always fired first in practice. Out of scope for the run-21 PR.
**Suggested approach:** Add `CYCLE_WALL_S = 3600` module constant; introduce a `_cycle_deadline` set on first `_request()` call per cycle; raise `BridgeStallError("cycle wall-clock exceeded")` when exceeded. Wire reset into `finalize_run()`.
**Co-land hint:** If Item 2 is also being implemented in the same window, land both in a single PR to avoid two rounds of churn in `scripts/cc_tool_bridge.py`.

## Item 4 — Remove dead `time.monotonic` monkeypatch in bridge test

**File:** `tests/test_cc_tool_bridge.py:270` (was `:264` in memory — the explanatory TODO comment block starts at line 262; the `monkeypatch.setattr` call is on line 270)
**Origin:** Reviewer cosmetic note; explicitly left as documentation of intent.
**Description:** The test monkeypatches `bridge_mod.time.monotonic`, but the stall predicate being asserted reads SQLite `julianday('now')` — not Python's monotonic clock. The patch fast-forwards the per-call deadline counter (harmless side effect) but is not load-bearing for the test's assertion.
**Why deferred:** Cosmetic; harmless but misleading to future readers.
**Suggested approach:** Preferred — delete `fake_monotonic`, the `monkeypatch.setattr` call, and the explanatory TODO comment block. Fallback — keep the patch and rewrite the comment to plainly state "kept to compress the per-call deadline counter; the stall assertion reads SQLite, not this clock."

## Item 5 — Add Bandit + pip-audit to in-clone CI mirror

**File:** `scripts/ci_runner.py` (new check-handlers needed; current file only handles tests + ruff)
**Origin:** Lesson from run 23 / PR#34 — Bandit B608 false-positive surfaced in GitHub Actions only after the PR was opened, requiring a follow-up recovery commit (`977c76b`).
**Description:** `ci_runner.run_ci_checks()` mirrors target-repo CI locally so Phase 5b agents can verify before opening a PR. Currently it runs `pytest` + `ruff check` + `ruff format --check`. It does NOT run Bandit or pip-audit, both of which are part of `atelier`'s GitHub Actions surface. Implementer prompts therefore can't catch security-scan failures in the clone.
**Why deferred (and why M not S):** Bandit/pip-audit failures are rare and recoverable post-PR; not blocking. But the implementation is heavier than the original S estimate — Bandit reads multiple config locations (incl. `bandit.yaml`), pip-audit has no pyproject section and must be detected via workflow YAML scan, Bandit's non-zero exit on findings must be distinguished from genuine crashes, and Phase 5b routing in `internal/cycle/SKILL.md` must be updated.
**Suggested approach:** Mirror the ruff opt-in pattern — detect Bandit via `pyproject.toml [tool.bandit]` / `.bandit` / `bandit.yaml`; detect pip-audit via target's `.github/workflows/` containing `pip-audit`. Add `bandit` and `pip_audit` result keys. Skip with `lint_warning`-style stubs when not configured. Consider an opt-out for offline pip-audit runs. Update `internal/cycle/SKILL.md` Phase 5b routing rules.

## Item 6 — Promote orphan-cleanup recipe into `scripts/cleanup_orphans.py`

**File:** New script (see existing `scripts/sweep_leaked_teams.py` for Layer 3 only); recipe documented in `docs/runbooks/orphan-teammate-cleanup.md`.
**Origin:** Directly downstream of PR#39 (commit `3a1251b`) which documented the 3-layer (process + pane + config) cleanup trifecta.
**Description:** Today an operator with orphans must run three manual steps in order: kill orphan processes, remove orphan tmux panes, then run `sweep_leaked_teams.py` to clean configs (or `rm -rf` the team config dirs). A single helper that walks all three layers in one call would close the manual gap.
**Why deferred:** Post-GAP-7 (smoke #4, run 27) runs don't produce orphans in normal operation, so the helper is only needed for pre-GAP-7 recovery or to defend against future regressions. Recipe is already documented; promoting to script is convenience.
**Suggested approach:** New `scripts/cleanup_orphans.py` exposing `cleanup_orphans(team_id_pattern=None, dry_run=True)`. Layer 1: `pgrep`-style process scan for `claude` children matching team-id naming. Layer 2: `tmux list-panes` filter on pane_pid matches. Layer 3: reuse `sweep_leaked_teams.find_orphan_team_ids()` and `rm -rf` matching config dirs. `--apply` mode without an explicit `team_id_pattern` MUST raise `ValueError` BEFORE invoking `pgrep`, `tmux`, or `rm` (no subprocess may be spawned in this failure mode). Document in the runbook.
