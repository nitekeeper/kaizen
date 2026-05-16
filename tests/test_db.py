import sqlite3
import pytest
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
