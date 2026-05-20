CREATE INDEX IF NOT EXISTS idx_runs_project_id ON runs(project_id);
CREATE INDEX IF NOT EXISTS idx_cycles_run_id ON cycles(run_id);
CREATE INDEX IF NOT EXISTS idx_abandonments_cycle_id ON abandonments(cycle_id);
