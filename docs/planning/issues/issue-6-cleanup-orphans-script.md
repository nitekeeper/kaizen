---
title: "[low] Promote orphan-cleanup recipe into scripts/cleanup_orphans.py"
labels: enhancement
---

## Context

Today an operator with orphan teammates must run three manual steps in order: kill orphan processes (Layer 1), remove orphan tmux panes (Layer 2), then run `sweep_leaked_teams.py` or `rm -rf` matching team config dirs (Layer 3). The recipe is documented in `docs/runbooks/orphan-teammate-cleanup.md` but not automated. A single helper that walks all three layers would close the manual gap.

Post-GAP-7 (smoke #4, run 27) the happy path doesn't produce orphans, so this is convenience for recovery and a defense against future regressions.

## Where

- New file: `scripts/cleanup_orphans.py`
- Reuses: `scripts/sweep_leaked_teams.py` (handles Layer 3 only today)
- Recipe source: `docs/runbooks/orphan-teammate-cleanup.md`

## Suggested approach

- Expose `cleanup_orphans(team_id_pattern: str | None = None, dry_run: bool = True)`
- Layer 1 — orphan processes: `pgrep`-style scan for `claude` children matching team-id naming
- Layer 2 — orphan panes: `tmux list-panes` filtered on `pane_pid` matches from Layer 1, or panes whose command is `claude` with an exited child
- Layer 3 — orphan configs: reuse `sweep_leaked_teams.find_orphan_team_ids()` plus `rm -rf` of matching config dirs
- CLI entrypoint: `python3 -m scripts.cleanup_orphans [--apply] [--team-id-pattern <regex>]` — default is dry-run for safety
- `--apply` mode without an explicit `team_id_pattern` MUST raise `ValueError` BEFORE invoking `pgrep`, `tmux`, or `rm` (no subprocess may be spawned in this failure mode)
- Update `docs/runbooks/orphan-teammate-cleanup.md` "Recovery" section to point to the helper while preserving manual instructions for fallback

## Acceptance criteria

- [ ] `scripts/cleanup_orphans.py` covers all three layers
- [ ] Default behavior is dry-run; `--apply` required for destructive cleanup
- [ ] `--apply` mode without an explicit `team_id_pattern` raises `ValueError` BEFORE invoking `pgrep`, `tmux`, or `rm` (no subprocess spawned in this failure mode)
- [ ] Test asserts `pytest.raises(ValueError)` for `--apply` without pattern, AND verifies (via a mocked `subprocess.run`) that no subprocess was spawned in that path
- [ ] Tests also cover dry-run output on a populated fake state and apply-mode against tmpdir fixtures
- [ ] `docs/runbooks/orphan-teammate-cleanup.md` references the new helper
- [ ] Cross-session limitation documented (see `feedback-cc-teamdelete-per-session.md`)

## Related

- Origin: PR#39 (commit `3a1251b`) — documented the 3-layer trifecta
- Builds on: `scripts/sweep_leaked_teams.py`
- Context doc: `docs/planning/deferred-todos.md` item 6
