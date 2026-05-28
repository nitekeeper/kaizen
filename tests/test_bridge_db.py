"""Tests for scripts/bridge_db.py — bootstrap idempotence + pragmas + purge."""

from __future__ import annotations

import sqlite3

import pytest

from scripts.bridge_db import bootstrap, purge_old_rows

# The pre-#86 bridge_requests schema: identical to current except its `kind`
# CHECK omits 'apply_layout'. Used to simulate a legacy DB for the
# constraint-upgrade migration test.
_LEGACY_BRIDGE_REQUESTS = """
CREATE TABLE bridge_requests (
    id            INTEGER PRIMARY KEY,
    run_id        INTEGER NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN
                  ('team_create','send_message','team_delete',
                   'cycle_done','aborted')),
    args_json     TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','ready','error')),
    response_json TEXT,
    error_text    TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at  TEXT
);
"""


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


def _seed_request(
    con,
    run_id: int,
    created_at_sql: str,
    status: str = "ready",
) -> int:
    """Insert a bridge_requests row with an explicit created_at expression.

    `created_at_sql` is a raw SQL expression (e.g. `datetime('now','-10 days')`)
    so we can backdate rows without round-tripping through Python's TZ-naive
    datetime handling.

    `status` defaults to `'ready'` (a terminal status) so age-based purge
    tests exercise the AGE predicate, not the pending-protection predicate
    added by the SDET-review-on-PR-for-#40-#45 follow-up. Pass
    ``status='pending'`` explicitly to test pending-row preservation.
    """
    cur = con.execute(
        f"INSERT INTO bridge_requests (run_id, kind, args_json, status, created_at) "
        f"VALUES (?, 'team_create', '{{}}', ?, {created_at_sql})",
        (run_id, status),
    )
    con.commit()
    return int(cur.lastrowid)


def test_purge_old_rows_deletes_only_aged_rows(bridge_path):
    bootstrap(str(bridge_path))
    con = sqlite3.connect(str(bridge_path))
    try:
        # Fresh row — 1 hour old. Must survive a 7-day purge.
        _seed_request(con, 1, "datetime('now','-1 hours')")
        # Old row — 10 days old. Must be deleted by a 7-day purge.
        _seed_request(con, 2, "datetime('now','-10 days')")
        # Heartbeat rows: fresh + stale.
        con.execute(
            "INSERT INTO bridge_heartbeat (run_id, last_polled_at) "
            "VALUES (10, datetime('now','-1 hours'))"
        )
        con.execute(
            "INSERT INTO bridge_heartbeat (run_id, last_polled_at) "
            "VALUES (11, datetime('now','-30 days'))"
        )
        con.execute(
            "INSERT INTO python_heartbeat (run_id, last_beat_at) "
            "VALUES (20, datetime('now','-2 hours'))"
        )
        con.execute(
            "INSERT INTO python_heartbeat (run_id, last_beat_at) "
            "VALUES (21, datetime('now','-365 days'))"
        )
        con.commit()

        deleted = purge_old_rows(con, cutoff_age_s=7 * 86400)

        assert deleted == {
            "bridge_requests": 1,
            "bridge_heartbeat": 1,
            "python_heartbeat": 1,
        }

        # Verify the survivors are the fresh ones.
        surviving_requests = {r[0] for r in con.execute("SELECT run_id FROM bridge_requests")}
        assert surviving_requests == {1}
        surviving_heartbeats = {r[0] for r in con.execute("SELECT run_id FROM bridge_heartbeat")}
        assert surviving_heartbeats == {10}
        surviving_python = {r[0] for r in con.execute("SELECT run_id FROM python_heartbeat")}
        assert surviving_python == {20}
    finally:
        con.close()


def test_purge_old_rows_returns_zero_counts_on_empty_tables(bridge_path):
    bootstrap(str(bridge_path))
    con = sqlite3.connect(str(bridge_path))
    try:
        deleted = purge_old_rows(con)
        assert deleted == {
            "bridge_requests": 0,
            "bridge_heartbeat": 0,
            "python_heartbeat": 0,
        }
    finally:
        con.close()


def test_purge_old_rows_honors_custom_cutoff(bridge_path):
    bootstrap(str(bridge_path))
    con = sqlite3.connect(str(bridge_path))
    try:
        _seed_request(con, 1, "datetime('now','-2 hours')")
        _seed_request(con, 2, "datetime('now','-30 minutes')")
        # 1-hour cutoff — only the 2-hours-old row should die.
        deleted = purge_old_rows(con, cutoff_age_s=3600)
        assert deleted["bridge_requests"] == 1
        survivors = {r[0] for r in con.execute("SELECT run_id FROM bridge_requests")}
        assert survivors == {2}
    finally:
        con.close()


def test_bootstrap_purges_old_rows_on_open(bridge_path):
    """bootstrap() must invoke purge_old_rows() with the default 7-day
    retention so stale rows from prior runs don't accumulate."""
    bootstrap(str(bridge_path))
    con = sqlite3.connect(str(bridge_path))
    try:
        _seed_request(con, 1, "datetime('now','-10 days')")
        _seed_request(con, 2, "datetime('now','-1 hours')")
    finally:
        con.close()

    # Re-bootstrap should trigger purge.
    bootstrap(str(bridge_path))

    con = sqlite3.connect(str(bridge_path))
    try:
        rows = {r[0] for r in con.execute("SELECT run_id FROM bridge_requests")}
    finally:
        con.close()
    assert rows == {2}, "bootstrap() should have purged the 10-day-old row"


def test_purge_preserves_pending_bridge_requests(bridge_path):
    """SDET-review on PR for #40-#45: an OLD bridge_requests row whose
    status is still 'pending' must NOT be purged — deleting it would
    yank an in-flight row out from under a still-active poller (which
    would then see 'row disappeared' and fail the cycle).

    Once the row moves to a terminal status (ready/error), it becomes
    eligible for purge by age like the rest.
    """
    bootstrap(str(bridge_path))
    con = sqlite3.connect(str(bridge_path))
    try:
        # Old (30 days) but still pending → MUST survive.
        cur = con.execute(
            "INSERT INTO bridge_requests (run_id, kind, args_json, status, created_at) "
            "VALUES (?, 'team_create', '{}', 'pending', datetime('now','-30 days'))",
            (1,),
        )
        pending_id = int(cur.lastrowid)
        con.commit()

        deleted = purge_old_rows(con, cutoff_age_s=7 * 86400)
        assert deleted["bridge_requests"] == 0, "pending row must not be purged regardless of age"
        survivors = {r[0] for r in con.execute("SELECT id FROM bridge_requests")}
        assert pending_id in survivors

        # Now mark the row 'ready' (a terminal status) and confirm the
        # next purge sweeps it.
        # CHECK constraint forbids arbitrary statuses; 'ready' is valid.
        con.execute(
            "UPDATE bridge_requests SET status='ready', completed_at=datetime('now') WHERE id = ?",
            (pending_id,),
        )
        con.commit()
        deleted = purge_old_rows(con, cutoff_age_s=7 * 86400)
        assert deleted["bridge_requests"] == 1, (
            "once status leaves 'pending', the row becomes purge-eligible by age"
        )
        survivors = {r[0] for r in con.execute("SELECT id FROM bridge_requests")}
        assert pending_id not in survivors
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


def test_bootstrap_upgrades_legacy_kind_constraint_preserving_rows(bridge_path):
    """kaizen#86: a bridge.db whose `kind` CHECK predates 'apply_layout' is
    rebuilt in place on bootstrap. Existing rows survive and apply_layout
    INSERTs become legal."""
    bridge_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(bridge_path))
    try:
        con.executescript(_LEGACY_BRIDGE_REQUESTS)
        con.execute(
            "INSERT INTO bridge_requests (id, run_id, kind, args_json) "
            "VALUES (7, 50, 'team_create', '{}')"
        )
        con.commit()
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO bridge_requests (run_id, kind, args_json) "
                "VALUES (50, 'apply_layout', '{}')"
            )
        con.rollback()
    finally:
        con.close()

    bootstrap(str(bridge_path))

    con = sqlite3.connect(str(bridge_path))
    try:
        # Legacy row preserved (id + kind intact).
        row = con.execute("SELECT run_id, kind FROM bridge_requests WHERE id = 7").fetchone()
        assert row == (50, "team_create")
        # apply_layout now accepted.
        con.execute(
            "INSERT INTO bridge_requests (run_id, kind, args_json) "
            "VALUES (50, 'apply_layout', '{}')"
        )
        con.commit()
        # Old table fully removed, not left dangling.
        assert "_bridge_requests_old" not in _table_names(con)
    finally:
        con.close()


def test_bootstrap_kind_upgrade_is_idempotent(bridge_path):
    """Running bootstrap twice on an already-current DB does not rebuild or
    drop rows (the migration is a no-op when 'apply_layout' is present)."""
    bootstrap(str(bridge_path))
    con = sqlite3.connect(str(bridge_path))
    try:
        con.execute(
            "INSERT INTO bridge_requests (run_id, kind, args_json) "
            "VALUES (50, 'apply_layout', '{}')"
        )
        con.commit()
    finally:
        con.close()

    bootstrap(str(bridge_path))  # second bootstrap must not touch the row

    con = sqlite3.connect(str(bridge_path))
    try:
        count = con.execute(
            "SELECT COUNT(*) FROM bridge_requests WHERE kind = 'apply_layout'"
        ).fetchone()[0]
        assert count == 1
    finally:
        con.close()
