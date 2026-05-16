import sqlite3
from contextlib import closing

import pytest

from scripts.db import get_connection
from scripts.migrate import apply_migrations, MIGRATIONS_DIR


def test_all_tables_created(tmp_path):
    """All 5 kaizen tables exist after running the migration set."""
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    with closing(get_connection(db_path)) as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()}
    expected = {"projects", "runs", "cycles", "abandonments", "migrations"}
    assert expected == tables


def test_migration_recorded(tmp_path):
    """The kaizen schema migration filename is recorded."""
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    with closing(get_connection(db_path)) as conn:
        row = conn.execute("SELECT filename FROM migrations").fetchone()
    assert row[0] == "001_kaizen_schema.sql"


def test_migration_is_idempotent(tmp_path):
    """Re-running migrations is a no-op and does not double-apply."""
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    apply_migrations(db_path, MIGRATIONS_DIR)  # must not raise
    with closing(get_connection(db_path)) as conn:
        count = conn.execute("SELECT COUNT(*) FROM migrations").fetchone()[0]
    assert count == 1


def _insert_project(conn, git_url="https://github.com/owner/repo.git", name="repo"):
    conn.execute(
        "INSERT INTO projects (git_url, name, test_command, read_paths, expert_roster, registered_at) "
        "VALUES (?, ?, 'pytest', '[]', '[]', datetime('now'))",
        (git_url, name),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


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
    with closing(get_connection(db_path)) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO runs (project_id, branch, cycles_requested, started_at, status) "
                "VALUES (99999, 'kaizen/test', 1, datetime('now'), 'running')"
            )
            conn.commit()


def test_cycles_fk_rejects_bogus_run_id(tmp_path):
    """Insert into cycles with non-existent run_id raises IntegrityError."""
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    with closing(get_connection(db_path)) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO cycles (run_id, cycle_n, status, started_at) "
                "VALUES (99999, 1, 'success', datetime('now'))"
            )
            conn.commit()


def test_abandonments_fk_rejects_bogus_cycle_id(tmp_path):
    """Insert into abandonments with non-existent cycle_id raises IntegrityError."""
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    with closing(get_connection(db_path)) as conn:
        with pytest.raises(sqlite3.IntegrityError):
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


def test_abandonments_reason_and_phase_are_free_text(tmp_path):
    """abandonments.reason and abandonments.phase_reached have no CHECK — accept any string."""
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
        conn.execute(
            "INSERT INTO cycles (run_id, cycle_n, status, started_at) "
            "VALUES (?, 1, 'abandoned', datetime('now'))",
            (run_id,),
        )
        cycle_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()

        # Arbitrary strings should be accepted.
        conn.execute(
            "INSERT INTO abandonments (cycle_id, phase_reached, reason, detail, created_at) "
            "VALUES (?, 'totally-made-up-phase', 'totally-made-up-reason', 'detail', datetime('now'))",
            (cycle_id,),
        )
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM abandonments WHERE cycle_id = ?", (cycle_id,)
        ).fetchone()[0]
        assert count == 1
