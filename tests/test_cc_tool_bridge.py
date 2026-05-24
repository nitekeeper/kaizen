"""Tests for scripts/cc_tool_bridge.py — QueueBridgeWrapper.

Covers:

  * The response_json contract for team_create / send_message /
    team_delete.
  * `python_heartbeat` is written every poll tick.
  * `BridgeStallError` when `bridge_heartbeat.last_polled_at` is older
    than HEARTBEAT_STALL_S.
  * `BridgeRemoteError` on `status='error'`.
  * Long-SendMessage no-stall regression guard — when S1's heartbeat
    advances per-row before each dispatch (the design's step-2a
    heartbeat poke), Python does NOT trip its stall detector even
    though the call takes 90s+ of simulated wall clock.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest

import scripts.cc_tool_bridge as bridge_mod
from scripts.bridge_db import bootstrap
from scripts.cc_tool_bridge import (
    BridgeRemoteError,
    BridgeStallError,
    QueueBridgeWrapper,
    queue_bridge_provider,
)


@pytest.fixture
def bridge_path(tmp_path):
    p = tmp_path / ".ai" / "bridge.db"
    bootstrap(str(p))
    return p


def _con(bridge_path):
    return sqlite3.connect(str(bridge_path))


def _tick_bridge_heartbeat(bridge_path, run_id, at_offset_seconds: float = 0.0):
    """UPSERT bridge_heartbeat. `at_offset_seconds` lets the test
    pretend the heartbeat is older than now."""
    con = _con(bridge_path)
    try:
        con.execute(
            "INSERT INTO bridge_heartbeat (run_id, last_polled_at, polled_count) "
            "VALUES (?, datetime('now', ?), 1) "
            "ON CONFLICT(run_id) DO UPDATE SET "
            "last_polled_at = datetime('now', ?), polled_count = polled_count + 1",
            (run_id, f"-{at_offset_seconds} seconds", f"-{at_offset_seconds} seconds"),
        )
        con.commit()
    finally:
        con.close()


def _mark_row_ready(bridge_path, row_id, response: dict):
    con = _con(bridge_path)
    try:
        con.execute(
            "UPDATE bridge_requests SET response_json = ?, status = 'ready', "
            "completed_at = datetime('now') WHERE id = ?",
            (json.dumps(response), row_id),
        )
        con.commit()
    finally:
        con.close()


def _mark_row_error(bridge_path, row_id, error_text: str):
    con = _con(bridge_path)
    try:
        con.execute(
            "UPDATE bridge_requests SET error_text = ?, status = 'error', "
            "completed_at = datetime('now') WHERE id = ?",
            (error_text, row_id),
        )
        con.commit()
    finally:
        con.close()


def _wait_for_pending_row(bridge_path, run_id, timeout=2.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        con = _con(bridge_path)
        try:
            cur = con.execute(
                "SELECT id FROM bridge_requests WHERE run_id=? AND status='pending' "
                "ORDER BY id LIMIT 1",
                (run_id,),
            )
            row = cur.fetchone()
        finally:
            con.close()
        if row is not None:
            return int(row[0])
        time.sleep(0.01)
    raise AssertionError("no pending row appeared")


# ── Constructor + bootstrap defence in depth ──────────────────────────────


def test_constructor_bootstraps_db(tmp_path):
    bridge_path = tmp_path / "bridge.db"
    # No prior bootstrap — wrapper must self-heal.
    QueueBridgeWrapper(str(bridge_path), run_id=1)
    con = sqlite3.connect(str(bridge_path))
    try:
        names = {
            r[0]
            for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    finally:
        con.close()
    assert {"bridge_requests", "bridge_heartbeat", "python_heartbeat"} <= names


# ── response_json contract round-trips ────────────────────────────────────


def test_team_create_round_trips(bridge_path):
    wrapper = QueueBridgeWrapper(str(bridge_path), run_id=1)
    # Pre-load: simulate an alive S1 that polled "just now."
    _tick_bridge_heartbeat(bridge_path, run_id=1, at_offset_seconds=0)

    def fake_s1():
        row_id = _wait_for_pending_row(bridge_path, run_id=1)
        _mark_row_ready(bridge_path, row_id, {"team_id": "team-xyz"})

    t = threading.Thread(target=fake_s1, daemon=True)
    t.start()
    team_id = wrapper.team_create(name="dev-team", members=["pm", "be-1"])
    t.join(timeout=5)
    assert team_id == "team-xyz"


def test_send_message_round_trips(bridge_path):
    wrapper = QueueBridgeWrapper(str(bridge_path), run_id=2)
    _tick_bridge_heartbeat(bridge_path, run_id=2, at_offset_seconds=0)

    def fake_s1():
        row_id = _wait_for_pending_row(bridge_path, run_id=2)
        _mark_row_ready(bridge_path, row_id, {"response": "got it"})

    t = threading.Thread(target=fake_s1, daemon=True)
    t.start()
    out = wrapper.send_message(team_id="team-xyz", to="pm", message="hi")
    t.join(timeout=5)
    assert out == "got it"


def test_team_delete_round_trips(bridge_path):
    wrapper = QueueBridgeWrapper(str(bridge_path), run_id=3)
    _tick_bridge_heartbeat(bridge_path, run_id=3, at_offset_seconds=0)

    def fake_s1():
        row_id = _wait_for_pending_row(bridge_path, run_id=3)
        _mark_row_ready(bridge_path, row_id, {})

    t = threading.Thread(target=fake_s1, daemon=True)
    t.start()
    # team_delete returns None.
    assert wrapper.team_delete("team-xyz") is None
    t.join(timeout=5)


# ── Heartbeat behaviour ───────────────────────────────────────────────────


def test_python_heartbeat_written_every_poll_tick(bridge_path):
    wrapper = QueueBridgeWrapper(str(bridge_path), run_id=4)
    _tick_bridge_heartbeat(bridge_path, run_id=4, at_offset_seconds=0)

    # Use a slow fake S1 so multiple poll ticks fire.
    def fake_s1():
        row_id = _wait_for_pending_row(bridge_path, run_id=4)
        time.sleep(0.7)  # ≥ 3 POLL_INTERVAL_S ticks (0.2s each)
        _mark_row_ready(bridge_path, row_id, {"team_id": "t-1"})

    t = threading.Thread(target=fake_s1, daemon=True)
    t.start()
    wrapper.team_create(name="x", members=[])
    t.join(timeout=5)

    con = _con(bridge_path)
    try:
        cur = con.execute("SELECT beat_count FROM python_heartbeat WHERE run_id=4")
        beat_count = cur.fetchone()[0]
    finally:
        con.close()
    # Python must have ticked AT LEAST twice (initial + at least one
    # poll loop iteration before the row was marked ready).
    assert beat_count >= 2, f"expected ≥ 2 python_heartbeat ticks, got {beat_count}"


def test_bridge_stall_raises_when_s1_heartbeat_old(bridge_path):
    wrapper = QueueBridgeWrapper(str(bridge_path), run_id=5)
    # Pretend S1's last poll was 600s ago — past HEARTBEAT_STALL_S=300s
    # (bumped in run-21 from the original 60s; see scripts/cc_tool_bridge.py
    # module-level comment).
    _tick_bridge_heartbeat(bridge_path, run_id=5, at_offset_seconds=600)

    with pytest.raises(BridgeStallError) as exc_info:
        wrapper.team_create(name="x", members=[])
    assert "stall" in str(exc_info.value).lower()


def test_heartbeat_stall_constants_match_run21_values():
    """Run-21 GAP-1 fix pin: HEARTBEAT_STALL_S=300 and PER_CALL_TIMEOUT_S=600.

    Locks both constants so a future drift back to the Rev-4 values
    (60 / 180) — which empirically spuriously-abandoned run 20 — fails
    loudly. See docs/kaizen/2026-05-24-bridge-smoke.md GAP-1.
    """
    assert bridge_mod.HEARTBEAT_STALL_S == 300.0
    assert bridge_mod.PER_CALL_TIMEOUT_S == 600.0
    # The class-level mirrors must also match (constructor binds these).
    assert QueueBridgeWrapper.HEARTBEAT_STALL_S == 300.0
    assert QueueBridgeWrapper.PER_CALL_TIMEOUT_S == 600.0


def test_long_sendmessage_does_not_trip_stall_at_old_threshold(bridge_path, monkeypatch):
    """Run-21 GAP-1 regression guard — the case run 20 hit empirically.

    A SendMessage round-trip taking ~90 seconds with NO interleaved S1
    heartbeat pokes (i.e. S1 is idle waiting for a `<teammate-message>`
    notification in CC team mode) must NOT trip `BridgeStallError` under
    the new HEARTBEAT_STALL_S=300 threshold. Run 20 saw 60.2s elapse →
    BridgeStallError under the old 60s threshold; this test reproduces
    that scenario and asserts the new threshold tolerates it.

    Compared to test_long_sendmessage_does_not_stall_when_s1_heartbeats_per_row:
    that test simulated the design's step-2a per-row heartbeat poke firing
    every few seconds. THIS test simulates the actual CC team-mode failure
    mode — S1 is idle for the entire 90s, NO heartbeat fires, and the only
    thing that saves the cycle is the bumped threshold itself.
    """
    wrapper = QueueBridgeWrapper(str(bridge_path), run_id=20)
    # Initial heartbeat: S1 polled once at t=0.
    _tick_bridge_heartbeat(bridge_path, run_id=20, at_offset_seconds=0)

    # Compress wall clock for the deadline check; keep the real DB clock
    # honest by simulating S1's heartbeat as "90s old" right before the row
    # is marked ready. The stall predicate compares julianday('now')
    # against last_polled_at — both real DB calls — so we set the DB
    # heartbeat to a fixed "90s ago" value once, mid-call.
    state = {"sim_elapsed": 0.0}
    real_monotonic = time.monotonic

    def fake_monotonic():
        return real_monotonic() + state["sim_elapsed"]

    # TODO(cosmetic): unused monkeypatch — stall predicate reads SQLite
    # julianday('now') against bridge_heartbeat.last_polled_at, NOT
    # Python's time.monotonic. The fake_monotonic above only fast-forwards
    # the per-call deadline counter, which is fine but not load-bearing
    # for the stall assertion this test makes. Reviewer noted but DEFER:
    # leaving the monkeypatch documents the intent (compress wall clock
    # so PER_CALL_TIMEOUT_S=600 isn't actually waited out) even though
    # the 90s simulated gap finishes well under the real deadline.
    monkeypatch.setattr(bridge_mod.time, "monotonic", fake_monotonic)

    def fake_s1():
        row_id = _wait_for_pending_row(bridge_path, run_id=20)
        # S1 does NOT heartbeat during the wait — this is the CC team-mode
        # async-SendMessage failure mode. Re-stamp the heartbeat row once
        # to "90s ago" so Python's julianday() check observes a 90s gap
        # (the empirical run-20 number, well past the old 60s threshold
        # but well under the new 300s threshold).
        _tick_bridge_heartbeat(bridge_path, run_id=20, at_offset_seconds=90)
        # Advance simulated monotonic so the per-call deadline budget is
        # accounted for; 90s is well under the new PER_CALL_TIMEOUT_S=600.
        state["sim_elapsed"] = 90.0
        _mark_row_ready(bridge_path, row_id, {"response": "finally back"})

    t = threading.Thread(target=fake_s1, daemon=True)
    t.start()
    # Must NOT raise BridgeStallError or BridgeTimeoutError.
    out = wrapper.send_message(team_id="t", to="agent", message="async-reply-test")
    t.join(timeout=10)
    assert out == "finally back"


def test_bridge_remote_error_on_status_error(bridge_path):
    wrapper = QueueBridgeWrapper(str(bridge_path), run_id=6)
    _tick_bridge_heartbeat(bridge_path, run_id=6, at_offset_seconds=0)

    def fake_s1():
        row_id = _wait_for_pending_row(bridge_path, run_id=6)
        _mark_row_error(bridge_path, row_id, "tool refused: 500")

    t = threading.Thread(target=fake_s1, daemon=True)
    t.start()
    with pytest.raises(BridgeRemoteError) as exc_info:
        wrapper.team_create(name="x", members=[])
    t.join(timeout=5)
    assert "tool refused" in str(exc_info.value)


# ── Long-SendMessage no-stall regression guard ────────────────────────────


def test_long_sendmessage_does_not_stall_when_s1_heartbeats_per_row(bridge_path, monkeypatch):
    """The MAJOR-HB60-SENDMSG regression guard.

    A 90-second SendMessage round-trip on the real wire would, without
    the per-row heartbeat poke, leave `bridge_heartbeat.last_polled_at`
    untouched for the entire 90s — Python's HEARTBEAT_STALL_S=60s
    detector would spuriously trip and abandon the cycle.

    We simulate the design's step-2a heartbeat poke by having the fake
    S1 advance `bridge_heartbeat.last_polled_at` immediately BEFORE
    finishing the long simulated tool call.

    We compress wall clock by stepping `time.monotonic()` from the
    test thread; the poll loop's `time.sleep(POLL_INTERVAL_S)` is
    short (0.2s) so the deadline check fires against simulated time,
    not real time.
    """
    wrapper = QueueBridgeWrapper(str(bridge_path), run_id=7)
    # Initial heartbeat: S1 just polled.
    _tick_bridge_heartbeat(bridge_path, run_id=7, at_offset_seconds=0)

    # Bound the test to a generous wall-clock budget; the simulated
    # 90s should be invisible to monotonic-based deadline checks if S1
    # keeps poking the heartbeat before the call returns.
    state = {"sim_elapsed": 0.0}
    real_monotonic = time.monotonic
    base = real_monotonic()

    def fake_monotonic():
        # Real elapsed PLUS the simulated 90s once "advanced."
        return real_monotonic() + state["sim_elapsed"]

    monkeypatch.setattr(bridge_mod.time, "monotonic", fake_monotonic)

    def fake_s1():
        # Drain the row: this S1 simulates a long-running SendMessage.
        row_id = _wait_for_pending_row(bridge_path, run_id=7)
        # Per the design's step-2a, S1 pokes the heartbeat EVERY couple
        # of seconds (≪ HEARTBEAT_STALL_S) while the tool is in flight.
        # We simulate this by re-poking every 0.05s real time while we
        # advance the simulated clock to 90s.
        target_sim = 90.0
        step = 5.0
        while state["sim_elapsed"] < target_sim:
            # Bump simulated clock and refresh S1's heartbeat to "now"
            # so its last_polled_at stays fresh against julianday().
            state["sim_elapsed"] += step
            _tick_bridge_heartbeat(bridge_path, run_id=7, at_offset_seconds=0)
            time.sleep(0.02)
        _mark_row_ready(bridge_path, row_id, {"response": "long but ok"})

    t = threading.Thread(target=fake_s1, daemon=True)
    t.start()
    out = wrapper.send_message(team_id="t", to="agent", message="long-running")
    t.join(timeout=10)
    assert out == "long but ok"
    # Sanity: deadline budget was actually exceeded in simulated time.
    assert state["sim_elapsed"] >= 90.0
    _ = base  # silence unused


# ── queue_bridge_provider ─────────────────────────────────────────────────


def test_queue_bridge_provider_returns_wrapper(bridge_path):
    provider = queue_bridge_provider(str(bridge_path), run_id=42)
    wrapper = provider(None, None, None, 1)
    assert isinstance(wrapper, QueueBridgeWrapper)
    assert wrapper._run_id == 42


def test_team_create_rejects_missing_team_id_in_response(bridge_path):
    wrapper = QueueBridgeWrapper(str(bridge_path), run_id=8)
    _tick_bridge_heartbeat(bridge_path, run_id=8, at_offset_seconds=0)

    def fake_s1():
        row_id = _wait_for_pending_row(bridge_path, run_id=8)
        _mark_row_ready(bridge_path, row_id, {"wrong_key": "x"})

    t = threading.Thread(target=fake_s1, daemon=True)
    t.start()
    with pytest.raises(BridgeRemoteError):
        wrapper.team_create("x", [])
    t.join(timeout=5)


def test_send_message_rejects_missing_response_in_response(bridge_path):
    wrapper = QueueBridgeWrapper(str(bridge_path), run_id=9)
    _tick_bridge_heartbeat(bridge_path, run_id=9, at_offset_seconds=0)

    def fake_s1():
        row_id = _wait_for_pending_row(bridge_path, run_id=9)
        _mark_row_ready(bridge_path, row_id, {"wrong": "x"})

    t = threading.Thread(target=fake_s1, daemon=True)
    t.start()
    with pytest.raises(BridgeRemoteError):
        wrapper.send_message("t", "to", "msg")
    t.join(timeout=5)
