import sqlite3
import stat
from pathlib import Path

from scripts.db import get_connection


def test_wal_mode_enabled(tmp_path):
    db_path = tmp_path / "test.db"
    conn = get_connection(str(db_path))
    row = conn.execute("PRAGMA journal_mode").fetchone()
    assert row[0] == "wal"
    conn.close()


def test_foreign_keys_enabled(tmp_path):
    db_path = tmp_path / "test.db"
    conn = get_connection(str(db_path))
    row = conn.execute("PRAGMA foreign_keys").fetchone()
    assert row[0] == 1
    conn.close()


def test_returns_connection(tmp_path):
    db_path = tmp_path / "test.db"
    conn = get_connection(str(db_path))
    assert isinstance(conn, sqlite3.Connection)
    conn.close()


def test_db_file_permissions_are_0600(tmp_path):
    db_path = str(tmp_path / "test.db")
    get_connection(db_path).close()
    mode = oct(stat.S_IMODE(Path(db_path).stat().st_mode))
    assert mode == oct(0o600), f"Expected 0600, got {mode}"
