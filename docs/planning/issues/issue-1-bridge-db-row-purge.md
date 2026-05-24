---
title: "[low] Cross-run bridge DB row purge"
labels: enhancement
---

## Context

`scripts/bridge_db.py::bootstrap()` is idempotent w.r.t. schema (every statement uses `CREATE TABLE IF NOT EXISTS`) but never deletes rows. Across many `kaizen:improve` invocations, stale `bridge_requests`, `bridge_heartbeat`, and `python_heartbeat` rows accumulate in `.ai/bridge.db`. After GAP-4 (batch wrapper) each cycle writes ~10x the rows it used to — empirical row-count delta observed in `bridge_requests` between runs 22 and 24. `STALE_ROW_S` handles per-run stale detection but no cross-run cleanup exists.

## Where

- `scripts/bridge_db.py:84` — TODO comment in `bootstrap()` docstring

## Suggested approach

- Pick a purge trigger — bootstrap-time age sweep is simplest; `finalize_run()` is another candidate
- Add a `purge_old_rows(cutoff_age_s)` helper using SQLite `julianday()` comparisons
- Decide retention window — suggest 7 days
- Update the `bootstrap()` docstring and `scripts/bridge_db.py` module header
- Test with a populated bridge DB across simulated runs

## Acceptance criteria

- [ ] `purge_old_rows()` (or equivalent) implemented with documented semantics
- [ ] Wired into either `bootstrap()` or `finalize_run()` (decision documented in code comment)
- [ ] Unit test covers: rows older than cutoff are deleted; rows within cutoff are preserved; rows from active runs are never deleted regardless of age
- [ ] TODO comment at `scripts/bridge_db.py:84` removed
- [ ] Module docstring documents the retention policy

## Related

- Origin: PR#36 review round 1 (marker `m3`)
- Context doc: `docs/planning/deferred-todos.md` item 1
