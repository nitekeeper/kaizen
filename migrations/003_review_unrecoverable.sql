-- 003_review_unrecoverable.sql
-- Add 'review_unrecoverable' to the abandonments.reason CHECK constraint.
-- SQLite does not support ALTER TABLE ... ADD CONSTRAINT, so the table must
-- be recreated with the updated constraint.

BEGIN;

ALTER TABLE abandonments RENAME TO abandonments_old;

CREATE TABLE abandonments (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle_id            INTEGER NOT NULL REFERENCES cycles(id) ON DELETE CASCADE,
  phase_reached       TEXT NOT NULL CHECK (phase_reached IN ('agenda','meeting','implementation','test')),
  reason              TEXT NOT NULL CHECK (reason IN ('no_consensus','destructive_rejected','tests_unrecoverable','review_unrecoverable','other')),
  detail              TEXT NOT NULL,
  report_memex_slug   TEXT,
  created_at          TEXT NOT NULL
);

INSERT INTO abandonments SELECT * FROM abandonments_old;
DROP TABLE abandonments_old;

-- Recreate the FK index that was on the original table (dropped when renaming).
CREATE INDEX IF NOT EXISTS idx_abandonments_cycle_id ON abandonments(cycle_id);

COMMIT;
