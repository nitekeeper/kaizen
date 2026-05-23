-- 004_phase_reached_review.sql
-- Phase 5b' substrate (per 3-agent audit 2026-05-23):
--   1. Add 'review' to phase_reached CHECK — fixes category-error bug in
--      internal/cycle/SKILL.md line 258 (review-loop abandonment was wrongly
--      flagged phase_reached='test').
--   2. Add 'push' to phase_reached CHECK — already documented in
--      internal/abandonment-report/SKILL.md:19 but never permitted in schema.
--   3. Add four nullable columns for structured review-abandonment fields:
--      review_iteration_count, unresolved_findings, convergence_summary,
--      reviewer_attribution. JSON-serialised in TEXT for the list/dict fields
--      (consistent with how read_paths / expert_roster are stored elsewhere).
-- SQLite cannot ALTER CHECK; recreate the table.

BEGIN;

ALTER TABLE abandonments RENAME TO abandonments_old;

CREATE TABLE abandonments (
  id                      INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle_id                INTEGER NOT NULL REFERENCES cycles(id) ON DELETE CASCADE,
  phase_reached           TEXT NOT NULL CHECK (phase_reached IN ('agenda','meeting','implementation','test','review','push')),
  reason                  TEXT NOT NULL CHECK (reason IN ('no_consensus','destructive_rejected','tests_unrecoverable','review_unrecoverable','other')),
  detail                  TEXT NOT NULL,
  report_memex_slug       TEXT,
  created_at              TEXT NOT NULL,
  review_iteration_count  INTEGER,
  unresolved_findings     TEXT,
  convergence_summary     TEXT,
  reviewer_attribution    TEXT
);

INSERT INTO abandonments (id, cycle_id, phase_reached, reason, detail, report_memex_slug, created_at)
SELECT id, cycle_id, phase_reached, reason, detail, report_memex_slug, created_at FROM abandonments_old;
DROP TABLE abandonments_old;

CREATE INDEX IF NOT EXISTS idx_abandonments_cycle_id ON abandonments(cycle_id);

COMMIT;
