"""Tests for scripts/sweep_leaked_teams.py — JSON1 orphan finder."""

from __future__ import annotations

import json
import sqlite3

import pytest

from scripts.bridge_db import bootstrap
from scripts.sweep_leaked_teams import (
    enqueue_aborted_row,
    find_orphan_team_ids,
)


@pytest.fixture
def bridge_path(tmp_path):
    p = tmp_path / ".ai" / "bridge.db"
    bootstrap(str(p))
    return p


def _seed_team_create(bridge_path, run_id: int, name: str, team_id: str) -> int:
    con = sqlite3.connect(str(bridge_path))
    try:
        cur = con.execute(
            "INSERT INTO bridge_requests (run_id, kind, args_json, response_json, status) "
            "VALUES (?, 'team_create', ?, ?, 'ready')",
            (
                run_id,
                json.dumps({"name": name, "members": []}),
                json.dumps({"team_id": team_id}),
            ),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def _seed_team_delete(bridge_path, run_id: int, team_id: str) -> int:
    con = sqlite3.connect(str(bridge_path))
    try:
        cur = con.execute(
            "INSERT INTO bridge_requests (run_id, kind, args_json, response_json, status) "
            "VALUES (?, 'team_delete', ?, '{}', 'ready')",
            (run_id, json.dumps({"team_id": team_id})),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def test_no_orphans_when_every_create_has_matching_delete(bridge_path):
    _seed_team_create(bridge_path, 1, "x", "team-x")
    _seed_team_delete(bridge_path, 1, "team-x")
    assert find_orphan_team_ids(str(bridge_path)) == []


def test_orphan_detected_when_delete_missing(bridge_path):
    _seed_team_create(bridge_path, 1, "x", "team-x")
    _seed_team_create(bridge_path, 2, "y", "team-y")
    _seed_team_delete(bridge_path, 2, "team-y")
    orphans = find_orphan_team_ids(str(bridge_path))
    assert orphans == [(1, "team-x")]


def test_errored_team_create_not_treated_as_orphan(bridge_path):
    """MINOR-JSON1-PATH contract: only `status='ready'` rows are
    considered candidates for orphan detection. An errored team_create
    has no valid response_json.team_id."""
    con = sqlite3.connect(str(bridge_path))
    try:
        con.execute(
            "INSERT INTO bridge_requests (run_id, kind, args_json, status, error_text) "
            "VALUES (1, 'team_create', '{\"name\":\"x\",\"members\":[]}', 'error', 'boom')"
        )
        con.commit()
    finally:
        con.close()
    assert find_orphan_team_ids(str(bridge_path)) == []


def test_enqueue_aborted_row_writes_team_ids_at_risk(bridge_path):
    row_id = enqueue_aborted_row(str(bridge_path), 99, ["team-a", "team-b"])
    con = sqlite3.connect(str(bridge_path))
    try:
        cur = con.execute(
            "SELECT run_id, kind, args_json, status FROM bridge_requests WHERE id=?",
            (row_id,),
        )
        run_id, kind, args_json, status = cur.fetchone()
    finally:
        con.close()
    assert run_id == 99
    assert kind == "aborted"
    assert status == "pending"
    parsed = json.loads(args_json)
    assert parsed["team_ids_at_risk"] == ["team-a", "team-b"]
    assert "reason" in parsed


def test_old_orphans_are_excluded_by_7_day_window(bridge_path):
    """m1 (review round 1): the orphan CTE filters `team_create` rows
    by `created_at >= datetime('now', '-7 days')`. Rows older than
    7 days must NOT appear in the orphan list — they would produce
    futile aborted enqueues."""
    con = sqlite3.connect(str(bridge_path))
    try:
        # Old orphan — 10 days ago. Must be excluded.
        con.execute(
            "INSERT INTO bridge_requests "
            "(run_id, kind, args_json, response_json, status, created_at) "
            "VALUES (1, 'team_create', ?, ?, 'ready', datetime('now', '-10 days'))",
            (
                json.dumps({"name": "old", "members": []}),
                json.dumps({"team_id": "team-old"}),
            ),
        )
        # Fresh orphan — 1 day ago. Must appear.
        con.execute(
            "INSERT INTO bridge_requests "
            "(run_id, kind, args_json, response_json, status, created_at) "
            "VALUES (2, 'team_create', ?, ?, 'ready', datetime('now', '-1 days'))",
            (
                json.dumps({"name": "fresh", "members": []}),
                json.dumps({"team_id": "team-fresh"}),
            ),
        )
        con.commit()
    finally:
        con.close()
    orphans = find_orphan_team_ids(str(bridge_path))
    team_ids = [tid for _, tid in orphans]
    assert "team-fresh" in team_ids
    assert "team-old" not in team_ids, "orphans older than 7 days must be excluded from the sweep"


def test_orphan_detection_uses_response_json_team_id_not_request_name(bridge_path):
    """The canonical team_id post-creation lives in `response_json.team_id`,
    NOT in `args_json.name` (which is just the requested name). Confirm
    the JSON1 paths target the right column."""
    _seed_team_create(bridge_path, 1, "human-readable-name", "team-uuid-xyz")
    _seed_team_delete(bridge_path, 1, "team-uuid-xyz")
    assert find_orphan_team_ids(str(bridge_path)) == []
    # But a delete keyed on the requested NAME does NOT match.
    _seed_team_create(bridge_path, 2, "name-only", "team-real-id")
    _seed_team_delete(bridge_path, 2, "name-only")  # wrong: delete by name
    orphans = find_orphan_team_ids(str(bridge_path))
    assert (2, "team-real-id") in orphans
