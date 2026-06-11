CREATE TABLE IF NOT EXISTS projects (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  git_url           TEXT NOT NULL UNIQUE,
  name              TEXT NOT NULL,
  base_branch       TEXT NOT NULL DEFAULT 'main',
  test_command      TEXT NOT NULL,
  read_paths        TEXT NOT NULL,
  expert_roster     TEXT NOT NULL,
  language          TEXT,
  registered_at     TEXT NOT NULL,
  last_run_at       TEXT,
  notes             TEXT
);

CREATE TABLE IF NOT EXISTS runs (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id        INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  branch            TEXT NOT NULL,
  pr_url            TEXT,
  cycles_requested  INTEGER NOT NULL,
  cycles_succeeded  INTEGER NOT NULL DEFAULT 0,
  cycles_abandoned  INTEGER NOT NULL DEFAULT 0,
  subject           TEXT,
  started_at        TEXT NOT NULL,
  ended_at          TEXT,
  status            TEXT NOT NULL CHECK (status IN ('running', 'complete', 'failed'))
);

CREATE TABLE IF NOT EXISTS cycles (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id              INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  cycle_n             INTEGER NOT NULL,
  subject             TEXT,
  status              TEXT NOT NULL CHECK (status IN ('success', 'abandoned')),
  commit_sha          TEXT,
  minutes_memex_slug  TEXT,
  started_at          TEXT NOT NULL,
  ended_at            TEXT,
  UNIQUE (run_id, cycle_n)
);

CREATE TABLE IF NOT EXISTS abandonments (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle_id            INTEGER NOT NULL REFERENCES cycles(id) ON DELETE CASCADE,
  phase_reached       TEXT NOT NULL CHECK (phase_reached IN ('agenda','meeting','implementation','test')),
  reason              TEXT NOT NULL CHECK (reason IN ('no_consensus','destructive_rejected','tests_unrecoverable','other')),
  detail              TEXT NOT NULL,
  report_memex_slug   TEXT,
  created_at          TEXT NOT NULL
);
