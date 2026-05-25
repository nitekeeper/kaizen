-- 005_reason_taxonomy.sql
-- F12 (audit cleanup): extend the abandonments.reason CHECK constraint with
-- three new categories so the cycle outcome can distinguish lint/security/sca
-- failures from generic "tests_unrecoverable". The old single bucket lumped
-- everything into a category that the abandonment-report consumer reads as
-- "the pytest run failed" — but a ruff or pip-audit fail is neither a pytest
-- run nor unrecoverable; surfacing the actual category lets triage land on
-- the right kind of follow-up faster.
--
-- New categories:
--   lint_failed     — ruff_check / ruff_format
--   security_failed — bandit
--   sca_failed      — pip_audit
--
-- Existing categories are preserved so historic rows still satisfy the CHECK.
-- SQLite cannot ALTER CHECK; recreate the table.

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
