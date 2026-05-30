-- 006_bridge_timeout_reason.sql
-- kaizen#93: add a dedicated 'bridge_timeout' abandonment reason so the
-- bridge-timeout family of abandonments (the run-53 incidents introduced by
-- kaizen#91 / PR #92 — per-call-timeout, wall-clock, and heartbeat-stall trips
-- on the read-first recoverable-artifact path) become filterable:
--
--   SELECT * FROM abandonments WHERE reason = 'bridge_timeout';
--
-- Previously these were bucketed into the generic 'other' reason (see
-- scripts/run.py::_bridge_timeout_to_abandoned_outcome), which forced triage to
-- grep the free-text detail column to separate a bridge trip from any other
-- 'other'. A first-class reason makes the category a queryable enum value.
--
-- This is a PURE ADDITIVE enum extension: the only structural change vs
-- migration 005 is adding 'bridge_timeout' to the reason CHECK IN(...) list.
-- Existing categories are preserved so historic rows still satisfy the CHECK.
-- SQLite cannot ALTER a CHECK constraint; recreate the table.

BEGIN;

ALTER TABLE abandonments RENAME TO abandonments_old;

CREATE TABLE abandonments (
  id                      INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle_id                INTEGER NOT NULL REFERENCES cycles(id) ON DELETE CASCADE,
  phase_reached           TEXT NOT NULL CHECK (phase_reached IN ('agenda','meeting','implementation','test','review','push')),
  reason                  TEXT NOT NULL CHECK (reason IN (
                              'no_consensus',
                              'destructive_rejected',
                              'tests_unrecoverable',
                              'review_unrecoverable',
                              'lint_failed',
                              'security_failed',
                              'sca_failed',
                              'bridge_timeout',
                              'other'
                          )),
  detail                  TEXT NOT NULL,
  report_memex_slug       TEXT,
  created_at              TEXT NOT NULL,
  review_iteration_count  INTEGER,
  unresolved_findings     TEXT,
  convergence_summary     TEXT,
  reviewer_attribution    TEXT
);

INSERT INTO abandonments (
    id, cycle_id, phase_reached, reason, detail, report_memex_slug, created_at,
    review_iteration_count, unresolved_findings, convergence_summary, reviewer_attribution
)
SELECT
    id, cycle_id, phase_reached, reason, detail, report_memex_slug, created_at,
    review_iteration_count, unresolved_findings, convergence_summary, reviewer_attribution
FROM abandonments_old;

DROP TABLE abandonments_old;

CREATE INDEX IF NOT EXISTS idx_abandonments_cycle_id ON abandonments(cycle_id);

COMMIT;
