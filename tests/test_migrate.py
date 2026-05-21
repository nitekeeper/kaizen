import sqlite3
from contextlib import closing

import pytest

from scripts.db import get_connection
from scripts.migrate import MIGRATIONS_DIR, apply_migrations


def test_all_tables_created(tmp_path):
    """All 5 kaizen tables exist after running the migration set."""
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    with closing(get_connection(db_path)) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
    expected = {"projects", "runs", "cycles", "abandonments", "migrations"}
    assert expected == tables


def test_migration_recorded(tmp_path):
    """All kaizen migration filenames are recorded."""
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    with closing(get_connection(db_path)) as conn:
        rows = conn.execute("SELECT filename FROM migrations ORDER BY filename").fetchall()
    filenames = [row[0] for row in rows]
    assert "001_kaizen_schema.sql" in filenames
    assert "002_add_fk_indexes.sql" in filenames


def test_migration_is_idempotent(tmp_path):
    """Re-running migrations is a no-op and does not double-apply."""
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    apply_migrations(db_path, MIGRATIONS_DIR)  # must not raise
    with closing(get_connection(db_path)) as conn:
        count = conn.execute("SELECT COUNT(*) FROM migrations").fetchone()[0]
    assert count == 2


def test_fk_indexes_created(tmp_path):
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    with closing(get_connection(db_path)) as conn:
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
    for expected_idx in ("idx_runs_project_id", "idx_cycles_run_id", "idx_abandonments_cycle_id"):
        assert expected_idx in indexes, f"Missing index {expected_idx!r}"


def test_apply_migrations_closes_connection_on_error(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    # Create a malformed migration that will fail executescript
    bad_dir = tmp_path / "migrations"
    bad_dir.mkdir()
    (bad_dir / "001_bad.sql").write_text("INVALID SQL HERE;;;")
    # Pre-fail check: function should raise but the connection must be closed
    import scripts.migrate as migrate

    with pytest.raises(sqlite3.Error):
        migrate.apply_migrations(db_path, bad_dir)
    # After raising, opening a fresh connection in WAL mode must not be blocked
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL").fetchone()
        # If connection was leaked, attempting an EXCLUSIVE write here would
        # fail with 'database is locked'. The fact that it succeeds confirms
        # the prior connection was properly closed.
        conn.execute("CREATE TABLE post_check (x INTEGER)")
        conn.commit()
    finally:
        conn.close()


def _insert_project(conn, git_url="https://github.com/owner/repo.git", name="repo"):
    conn.execute(
        "INSERT INTO projects (git_url, name, test_command, read_paths, expert_roster, registered_at) "
        "VALUES (?, ?, 'pytest', '[]', '[]', datetime('now'))",
        (git_url, name),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _insert_run_and_cycle(conn, project_id):
    """Insert a minimal run + cycle pair under the given project_id; return cycle_id."""
    conn.execute(
        "INSERT INTO runs (project_id, branch, cycles_requested, started_at, status) "
        "VALUES (?, 'kaizen/test', 1, datetime('now'), 'running')",
        (project_id,),
    )
    run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO cycles (run_id, cycle_n, status, started_at) "
        "VALUES (?, 1, 'abandoned', datetime('now'))",
        (run_id,),
    )
    cycle_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    return cycle_id


def test_projects_git_url_unique(tmp_path):
    """projects.git_url is UNIQUE — duplicate insert raises IntegrityError."""
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    with closing(get_connection(db_path)) as conn:
        _insert_project(conn, git_url="https://github.com/owner/repo.git")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_project(conn, git_url="https://github.com/owner/repo.git", name="dup")


def test_runs_fk_rejects_bogus_project_id(tmp_path):
    """Insert into runs with non-existent project_id raises IntegrityError."""
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    with closing(get_connection(db_path)) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO runs (project_id, branch, cycles_requested, started_at, status) "
            "VALUES (99999, 'kaizen/test', 1, datetime('now'), 'running')"
        )
        conn.commit()


def test_cycles_fk_rejects_bogus_run_id(tmp_path):
    """Insert into cycles with non-existent run_id raises IntegrityError."""
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    with closing(get_connection(db_path)) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO cycles (run_id, cycle_n, status, started_at) "
            "VALUES (99999, 1, 'success', datetime('now'))"
        )
        conn.commit()


def test_abandonments_fk_rejects_bogus_cycle_id(tmp_path):
    """Insert into abandonments with non-existent cycle_id raises IntegrityError."""
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    with closing(get_connection(db_path)) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO abandonments (cycle_id, phase_reached, reason, detail, created_at) "
            "VALUES (99999, 'meeting', 'no_consensus', 'test', datetime('now'))"
        )
        conn.commit()


def test_cycles_status_check_rejects_invalid(tmp_path):
    """cycles.status CHECK rejects values outside ('success', 'abandoned')."""
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    with closing(get_connection(db_path)) as conn:
        project_id = _insert_project(conn)
        conn.execute(
            "INSERT INTO runs (project_id, branch, cycles_requested, started_at, status) "
            "VALUES (?, 'kaizen/test', 1, datetime('now'), 'running')",
            (project_id,),
        )
        run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO cycles (run_id, cycle_n, status, started_at) "
                "VALUES (?, 1, 'foo', datetime('now'))",
                (run_id,),
            )
            conn.commit()


def test_runs_status_check_rejects_invalid(tmp_path):
    """runs.status CHECK rejects values outside ('running','complete','failed')."""
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    with closing(get_connection(db_path)) as conn:
        project_id = _insert_project(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO runs (project_id, branch, cycles_requested, started_at, status) "
                "VALUES (?, 'kaizen/test', 1, datetime('now'), 'bogus')",
                (project_id,),
            )
            conn.commit()


def test_abandonments_phase_reached_check_rejects_invalid(tmp_path):
    """abandonments.phase_reached CHECK rejects values outside ('agenda','meeting','implementation','test')."""
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    with closing(get_connection(db_path)) as conn:
        project_id = _insert_project(conn)
        cycle_id = _insert_run_and_cycle(conn, project_id)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO abandonments (cycle_id, phase_reached, reason, detail, created_at) "
                "VALUES (?, 'invalid-phase', 'no_consensus', 'detail', datetime('now'))",
                (cycle_id,),
            )
            conn.commit()


def test_abandonments_reason_check_rejects_push_failed(tmp_path):
    """push_failed is a run-level event, not a cycle abandonment reason."""
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    with closing(get_connection(db_path)) as conn:
        project_id = _insert_project(conn)
        cycle_id = _insert_run_and_cycle(conn, project_id)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO abandonments (cycle_id, phase_reached, reason, detail, created_at) "
                "VALUES (?, 'meeting', 'push_failed', 'detail', datetime('now'))",
                (cycle_id,),
            )
            conn.commit()


def test_abandonments_valid_values_accepted(tmp_path):
    """All 16 valid (phase_reached x reason) combinations must succeed."""
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    with closing(get_connection(db_path)) as conn:
        project_id = _insert_project(conn)
        cycle_id = _insert_run_and_cycle(conn, project_id)
        valid_phases = ("agenda", "meeting", "implementation", "test")
        valid_reasons = ("no_consensus", "destructive_rejected", "tests_unrecoverable", "other")
        for phase in valid_phases:
            for reason in valid_reasons:
                conn.execute(
                    "INSERT INTO abandonments (cycle_id, phase_reached, reason, detail, created_at) "
                    "VALUES (?, ?, ?, 'detail', datetime('now'))",
                    (cycle_id, phase, reason),
                )
        conn.commit()
