"""Tests for scripts/bridge_write.py — the SOLE write path for response_json.

Includes the injection battery exercising every named attack string
from the design's Test Plan section (line 789 of
`docs/design/python-cc-tool-bridge-design.md`).
"""

from __future__ import annotations

import io
import json
import sqlite3
import sys

import pytest

from scripts.bridge_db import bootstrap
from scripts.bridge_write import main as bridge_write_main


@pytest.fixture
def bridge_path(tmp_path):
    p = tmp_path / ".ai" / "bridge.db"
    bootstrap(str(p))
    return p


def _seed_pending_row(bridge_path, kind="send_message", run_id=1) -> int:
    con = sqlite3.connect(str(bridge_path))
    try:
        cur = con.execute(
            "INSERT INTO bridge_requests (run_id, kind, args_json, status) "
            "VALUES (?, ?, ?, 'pending')",
            (run_id, kind, "{}"),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def _read_row(bridge_path, row_id):
    con = sqlite3.connect(str(bridge_path))
    try:
        cur = con.execute(
            "SELECT status, response_json, error_text FROM bridge_requests WHERE id=?",
            (row_id,),
        )
        return cur.fetchone()
    finally:
        con.close()


def _invoke(bridge_path, row_id, status, body, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(body))
    return bridge_write_main(
        ["--row-id", str(row_id), "--status", status, "--bridge-db", str(bridge_path)]
    )


# ── Happy path ────────────────────────────────────────────────────────────


def test_happy_path_writes_response_json(bridge_path, monkeypatch):
    rid = _seed_pending_row(bridge_path)
    body = json.dumps({"team_id": "t-abc"})
    code = _invoke(bridge_path, rid, "ready", body, monkeypatch)
    assert code == 0
    status, response_json, error_text = _read_row(bridge_path, rid)
    assert status == "ready"
    assert response_json == body
    assert error_text is None


def test_happy_path_error_status_writes_error_text(bridge_path, monkeypatch):
    rid = _seed_pending_row(bridge_path)
    code = _invoke(bridge_path, rid, "error", "tool refused: 500", monkeypatch)
    assert code == 0
    status, response_json, error_text = _read_row(bridge_path, rid)
    assert status == "error"
    assert response_json is None
    assert error_text == "tool refused: 500"


# ── Refusal cases ─────────────────────────────────────────────────────────


def test_refuses_non_json_ready_body(bridge_path, monkeypatch):
    rid = _seed_pending_row(bridge_path)
    code = _invoke(bridge_path, rid, "ready", "not json at all", monkeypatch)
    assert code == 2
    # Row must remain pending — the broken write did NOT land.
    status, response_json, error_text = _read_row(bridge_path, rid)
    assert status == "pending"
    assert response_json is None
    assert error_text is None


def test_refuses_missing_row(bridge_path, monkeypatch):
    code = _invoke(bridge_path, 999_999, "ready", "{}", monkeypatch)
    assert code == 3


def test_refuses_double_write_when_status_not_pending(bridge_path, monkeypatch):
    rid = _seed_pending_row(bridge_path)
    code1 = _invoke(bridge_path, rid, "ready", json.dumps({"ok": True}), monkeypatch)
    assert code1 == 0
    # Second write must REFUSE — row is now 'ready', not 'pending'.
    code2 = _invoke(bridge_path, rid, "ready", json.dumps({"hijack": "attempt"}), monkeypatch)
    assert code2 == 4
    # The first payload survives.
    status, response_json, _ = _read_row(bridge_path, rid)
    assert status == "ready"
    assert json.loads(response_json) == {"ok": True}


def test_argparse_rejects_unknown_status(bridge_path, monkeypatch):
    # argparse `choices=("ready","error")` makes any other value an
    # argparse error — exit 2 from SystemExit.
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    with pytest.raises(SystemExit):
        bridge_write_main(
            [
                "--row-id",
                "1",
                "--status",
                "MALICIOUS_status",
                "--bridge-db",
                str(bridge_path),
            ]
        )


# ── Injection battery ─────────────────────────────────────────────────────
#
# Each test confirms the named attack string lands as LITERAL DATA in
# response_json and the queue table remains intact afterwards.


@pytest.mark.parametrize(
    "attack",
    [
        # Classic SQL injection
        "'; DROP TABLE bridge_requests; --",
        # Shell injection
        "$(rm -rf /)",
        # Backtick command sub
        "`echo pwned`",
        # Embedded newlines + SQL
        "line1\n'; DELETE FROM bridge_requests; --\nline3",
        # Unicode quote variants — explicit \u escapes keep ruff RUF001
        # off the source line while still exercising the design-doc-
        # named attack class verbatim at runtime (U+201C/U+201D smart
        # double quotes, U+2018/U+2019 smart single quotes, U+202E RTL
        # override).
        "\u201cfoo\u201d \u2018bar\u2019 \u202etxt.evil.exe",
        # JSON with unescaped doublequotes (must come through as the
        # exact body when wrapped as a JSON string field below).
        'has "embedded" quotes',
        # Null bytes (SQLite stores them as literal text)
        "before\x00after",
    ],
)
def test_injection_battery_preserves_queue_and_writes_verbatim(bridge_path, monkeypatch, attack):
    rid = _seed_pending_row(bridge_path)
    # Wrap each attack as the `response` field of a valid JSON object so
    # the --status ready JSON validation passes; the attack text lives
    # inside.
    body = json.dumps({"response": attack})
    code = _invoke(bridge_path, rid, "ready", body, monkeypatch)
    assert code == 0, f"injection {attack!r} broke the helper (exit {code})"

    # Queue table is intact — DROP TABLE / DELETE injections were ignored.
    con = sqlite3.connect(str(bridge_path))
    try:
        cur = con.execute("SELECT COUNT(*) FROM bridge_requests")
        assert cur.fetchone()[0] >= 1, f"bridge_requests was tampered with by attack {attack!r}"
        cur = con.execute("SELECT response_json FROM bridge_requests WHERE id=?", (rid,))
        stored = cur.fetchone()[0]
    finally:
        con.close()

    # The exact bytes we wrote must round-trip — parameter binding means
    # the payload is treated as opaque DATA, not parsed-as-SQL.
    assert stored == body
    # And the inner attack text decodes back to the original literal.
    assert json.loads(stored)["response"] == attack


@pytest.mark.parametrize(
    "attack",
    [
        # m6 (review round 1): the error path MUST get the same
        # 7-string injection battery the ready path receives. Same
        # attack list as test_injection_battery_preserves_queue_and_
        # writes_verbatim — kept independent so divergence is caught.
        "'; DROP TABLE bridge_requests; --",
        "$(rm -rf /)",
        "`echo pwned`",
        "line1\n'; DELETE FROM bridge_requests; --\nline3",
        "\u201cfoo\u201d \u2018bar\u2019 \u202etxt.evil.exe",
        'has "embedded" quotes',
        "before\x00after",
    ],
)
def test_injection_battery_error_path_preserves_queue_and_writes_verbatim(
    bridge_path, monkeypatch, attack
):
    rid = _seed_pending_row(bridge_path)
    code = _invoke(bridge_path, rid, "error", attack, monkeypatch)
    assert code == 0, f"injection {attack!r} broke the helper (exit {code})"

    con = sqlite3.connect(str(bridge_path))
    try:
        cur = con.execute("SELECT COUNT(*) FROM bridge_requests")
        assert cur.fetchone()[0] >= 1, f"bridge_requests was tampered with by attack {attack!r}"
        cur = con.execute("SELECT status, error_text FROM bridge_requests WHERE id=?", (rid,))
        status, stored = cur.fetchone()
    finally:
        con.close()
    assert status == "error"
    assert stored == attack


def test_injection_in_error_path_also_safe(bridge_path, monkeypatch):
    rid = _seed_pending_row(bridge_path)
    attack = "'; DROP TABLE bridge_requests; --\nshould not execute"
    code = _invoke(bridge_path, rid, "error", attack, monkeypatch)
    assert code == 0
    status, _, error_text = _read_row(bridge_path, rid)
    assert status == "error"
    assert error_text == attack
    # Table still exists.
    con = sqlite3.connect(str(bridge_path))
    try:
        cur = con.execute("SELECT COUNT(*) FROM bridge_requests")
        assert cur.fetchone()[0] >= 1
    finally:
        con.close()


def test_source_uses_only_parameter_binding(monkeypatch):
    """Defensive: grep the helper's source for any f-string INSERT/UPDATE
    that interpolates data. Only the trusted `col` value (from the
    argparse enum) may be interpolated."""
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "scripts" / "bridge_write.py"
    text = src.read_text(encoding="utf-8")
    # No INSERT statement should be f-string interpolated at all.
    assert 'f"INSERT' not in text and "f'INSERT" not in text
    # The ONE UPDATE f-string is permitted because it interpolates only
    # `col`, which comes from `choices=("ready","error")`.
    # Pin the exact text of the UPDATE so future regressions are caught.
    assert 'f"UPDATE bridge_requests SET {col} = ?, status = ?, "' in text
