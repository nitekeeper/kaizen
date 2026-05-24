"""Tests for scripts/bridge_db.py — bootstrap idempotence + pragmas."""

from __future__ import annotations

import sqlite3

import pytest

from scripts.bridge_db import bootstrap


@pytest.fixture
def bridge_path(tmp_path):
    return tmp_path / ".ai" / "bridge.db"


def _table_names(con: sqlite3.Connection) -> set[str]:
    cur = con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {r[0] for r in cur.fetchall()}


def test_bootstrap_creates_three_tables(bridge_path):
    bootstrap(str(bridge_path))
    con = sqlite3.connect(str(bridge_path))
    try:
        names = _table_names(con)
    finally:
        con.close()
    assert {"bridge_requests", "bridge_heartbeat", "python_heartbeat"} <= names


def test_bootstrap_creates_parent_directory(tmp_path):
    nested = tmp_path / "deep" / "nested" / "bridge.db"
    assert not nested.parent.exists()
    bootstrap(str(nested))
    assert nested.exists()


def test_bootstrap_is_idempotent(bridge_path):
    bootstrap(str(bridge_path))
    bootstrap(str(bridge_path))
    bootstrap(str(bridge_path))
    # All three tables still present, schema still queryable.
    con = sqlite3.connect(str(bridge_path))
    try:
        names = _table_names(con)
    finally:
        con.close()
    assert {"bridge_requests", "bridge_heartbeat", "python_heartbeat"} <= names


def test_bootstrap_does_not_clobber_existing_rows(bridge_path):
    bootstrap(str(bridge_path))
    con = sqlite3.connect(str(bridge_path))
    try:
        con.execute(
            "INSERT INTO bridge_requests (run_id, kind, args_json) VALUES (?, ?, ?)",
            (1, "team_create", '{"name":"x","members":[]}'),
        )
        con.commit()
    finally:
        con.close()
    bootstrap(str(bridge_path))  # idempotent re-run
    con = sqlite3.connect(str(bridge_path))
    try:
        cur = con.execute("SELECT COUNT(*) FROM bridge_requests")
        assert cur.fetchone()[0] == 1
    finally:
        con.close()


def test_bootstrap_enables_wal_journal_mode(bridge_path):
    bootstrap(str(bridge_path))
    con = sqlite3.connect(str(bridge_path))
    try:
        cur = con.execute("PRAGMA journal_mode")
        mode = cur.fetchone()[0]
    finally:
        con.close()
    # WAL persists across opens, so the post-bootstrap journal_mode
    # must remain 'wal'.
    assert mode.lower() == "wal"


def test_bootstrap_schema_has_pending_default_status(bridge_path):
    bootstrap(str(bridge_path))
    con = sqlite3.connect(str(bridge_path))
    try:
        con.execute(
            "INSERT INTO bridge_requests (run_id, kind, args_json) VALUES (?, ?, ?)",
            (7, "team_create", '{"name":"x","members":[]}'),
        )
        con.commit()
        cur = con.execute("SELECT status FROM bridge_requests WHERE run_id=7")
        assert cur.fetchone()[0] == "pending"
    finally:
        con.close()


def test_bootstrap_rejects_invalid_kind(bridge_path):
    bootstrap(str(bridge_path))
    con = sqlite3.connect(str(bridge_path))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO bridge_requests (run_id, kind, args_json) VALUES (?, ?, ?)",
                (1, "definitely_not_a_kind", "{}"),
            )
    finally:
        con.close()


def test_bootstrap_rejects_invalid_status(bridge_path):
    bootstrap(str(bridge_path))
    con = sqlite3.connect(str(bridge_path))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO bridge_requests (run_id, kind, args_json, status) VALUES (?, ?, ?, ?)",
                (1, "team_create", "{}", "bogus_status"),
            )
    finally:
        con.close()


def test_heartbeat_tables_use_run_id_primary_key(bridge_path):
    bootstrap(str(bridge_path))
    con = sqlite3.connect(str(bridge_path))
    try:
        con.execute(
            "INSERT INTO bridge_heartbeat (run_id, last_polled_at) VALUES (1, '2026-01-01')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            # Duplicate run_id must violate PK.
            con.execute(
                "INSERT INTO bridge_heartbeat (run_id, last_polled_at) VALUES (1, '2026-01-02')"
            )
        con.execute("INSERT INTO python_heartbeat (run_id, last_beat_at) VALUES (1, '2026-01-01')")
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO python_heartbeat (run_id, last_beat_at) VALUES (1, '2026-01-02')"
            )
    finally:
        con.close()
