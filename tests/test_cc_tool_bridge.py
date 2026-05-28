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


def test_apply_layout_round_trips_and_enqueues_apply_layout_kind(bridge_path):
    """kaizen#86: apply_layout enqueues an `apply_layout` bridge row the
    orchestrator services (it runs scripts.fold_workspace), and returns None."""
    wrapper = QueueBridgeWrapper(str(bridge_path), run_id=86)
    _tick_bridge_heartbeat(bridge_path, run_id=86, at_offset_seconds=0)
    captured: dict[str, str] = {}

    def fake_s1():
        row_id = _wait_for_pending_row(bridge_path, run_id=86)
        con = sqlite3.connect(str(bridge_path))
        try:
            captured["kind"] = con.execute(
                "SELECT kind FROM bridge_requests WHERE id=?", (row_id,)
            ).fetchone()[0]
        finally:
            con.close()
        _mark_row_ready(bridge_path, row_id, {})

    t = threading.Thread(target=fake_s1, daemon=True)
    t.start()
    assert wrapper.apply_layout("kaizen-cycle-86-1") is None
    t.join(timeout=5)
    assert captured["kind"] == "apply_layout"


def test_apply_layout_is_best_effort_on_timeout(bridge_path):
    """A layout that never gets serviced must NOT raise — it is cosmetic and
    must never abort the cycle (mirrors team_delete best-effort)."""
    wrapper = QueueBridgeWrapper(str(bridge_path), run_id=87)
    wrapper.CLEANUP_TIMEOUT_S = 0.3
    wrapper.POLL_INTERVAL_S = 0.02
    _tick_bridge_heartbeat(bridge_path, run_id=87, at_offset_seconds=0)
    # No servicing thread → the apply_layout row times out; apply_layout swallows it.
    assert wrapper.apply_layout("never-serviced") is None


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
    """Issue #41 + SDET-review-PR follow-up: the stall predicate now
    requires BOTH clocks to agree. To exercise the raise branch we
    pre-seed `_last_python_tick_monotonic[run_id]` with an OLD monotonic
    timestamp BEFORE constructing the wrapper, simulating "Python was
    ticking, then stopped" (a real S1 stall, not a suspend-resume).
    Without the pre-seed, mono_gap would be None and the predicate
    correctly falls through to the suspend-resume branch."""
    run_id = 5
    # Simulate prior monotonic activity that then went silent.
    bridge_mod._last_python_tick_monotonic[run_id] = time.monotonic() - 600.0
    wrapper = QueueBridgeWrapper(str(bridge_path), run_id=run_id)
    # Pretend S1's last poll was 600s ago — past HEARTBEAT_STALL_S=300s
    # (bumped in run-21 from the original 60s; see scripts/cc_tool_bridge.py
    # module-level comment).
    _tick_bridge_heartbeat(bridge_path, run_id=run_id, at_offset_seconds=600)

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


def test_long_sendmessage_does_not_trip_stall_at_old_threshold(bridge_path):
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

    # The stall predicate compares SQLite julianday('now') against
    # bridge_heartbeat.last_polled_at — both real DB calls. Simulate a
    # 90s heartbeat gap by re-stamping last_polled_at mid-call to "90s
    # ago"; the actual call completes in real time well under the
    # PER_CALL_TIMEOUT_S=600 deadline.

    def fake_s1():
        row_id = _wait_for_pending_row(bridge_path, run_id=20)
        # S1 does NOT heartbeat during the wait — this is the CC team-mode
        # async-SendMessage failure mode. Re-stamp the heartbeat row once
        # to "90s ago" so Python's julianday() check observes a 90s gap
        # (the empirical run-20 number, well past the old 60s threshold
        # but well under the new 300s threshold).
        _tick_bridge_heartbeat(bridge_path, run_id=20, at_offset_seconds=90)
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


# ── send_message_many (GAP-4 fix) ─────────────────────────────────────────


def _all_pending_rows(bridge_path, run_id) -> list[int]:
    con = _con(bridge_path)
    try:
        cur = con.execute(
            "SELECT id FROM bridge_requests WHERE run_id=? AND status='pending' ORDER BY id",
            (run_id,),
        )
        return [int(r[0]) for r in cur.fetchall()]
    finally:
        con.close()


def _count_rows(bridge_path, run_id) -> int:
    con = _con(bridge_path)
    try:
        cur = con.execute(
            "SELECT COUNT(*) FROM bridge_requests WHERE run_id=?",
            (run_id,),
        )
        return int(cur.fetchone()[0])
    finally:
        con.close()


def test_send_message_many_dispatches_in_parallel(bridge_path):
    """GAP-4 (docs/kaizen/2026-05-24-bridge-smoke-2.md): batch dispatch.

    Asserts:
      * Three messages enqueued in ONE batch → exactly 3 rows appear
        before any response is written (proves single-transaction enqueue,
        not interleaved-with-poll one-by-one).
      * Responses come back in INPUT order even when the fake S1 marks
        them ready in a SCRAMBLED order (m2 first, m1 second, m3 last).
    """
    wrapper = QueueBridgeWrapper(str(bridge_path), run_id=100)
    _tick_bridge_heartbeat(bridge_path, run_id=100, at_offset_seconds=0)

    started_event = threading.Event()
    row_count_when_started: list[int] = []

    def fake_s1():
        # Wait until all 3 rows are visible before marking any ready —
        # this is the single-transaction invariant under test.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            pending = _all_pending_rows(bridge_path, run_id=100)
            if len(pending) >= 3:
                row_count_when_started.append(_count_rows(bridge_path, run_id=100))
                started_event.set()
                # Mark in scrambled order: m2 → m1 → m3 to prove input-order
                # preservation of the wrapper's return.
                _mark_row_ready(bridge_path, pending[1], {"response": "resp2"})
                _mark_row_ready(bridge_path, pending[0], {"response": "resp1"})
                _mark_row_ready(bridge_path, pending[2], {"response": "resp3"})
                return
            time.sleep(0.01)
        raise AssertionError("never saw 3 pending rows")

    t = threading.Thread(target=fake_s1, daemon=True)
    t.start()
    out = wrapper.send_message_many(
        [
            {"team_id": "t1", "to": "a", "message": "msg1"},
            {"team_id": "t1", "to": "b", "message": "msg2"},
            {"team_id": "t1", "to": "c", "message": "msg3"},
        ]
    )
    t.join(timeout=5)

    assert started_event.is_set(), "fake_s1 never saw 3 pending rows"
    # Single-batch INSERT: exactly 3 rows when the marker fired.
    assert row_count_when_started == [3], (
        f"expected exactly 3 rows at the parallel-dispatch instant, got {row_count_when_started}"
    )
    # Input-order preservation despite scrambled mark-ready order.
    assert out == ["resp1", "resp2", "resp3"], (
        f"send_message_many must return responses in input order; got {out}"
    )


def test_send_message_many_propagates_errors(bridge_path):
    """GAP-4: if any row in the batch errors, BridgeRemoteError names the
    input index AND the failing recipient."""
    wrapper = QueueBridgeWrapper(str(bridge_path), run_id=101)
    _tick_bridge_heartbeat(bridge_path, run_id=101, at_offset_seconds=0)

    def fake_s1():
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            pending = _all_pending_rows(bridge_path, run_id=101)
            if len(pending) >= 3:
                # m2 (input index 1) errors.
                _mark_row_error(bridge_path, pending[1], "boom")
                return
            time.sleep(0.01)
        raise AssertionError("never saw 3 pending rows")

    t = threading.Thread(target=fake_s1, daemon=True)
    t.start()
    with pytest.raises(BridgeRemoteError) as exc_info:
        wrapper.send_message_many(
            [
                {"team_id": "t1", "to": "a", "message": "msg1"},
                {"team_id": "t1", "to": "b", "message": "msg2"},
                {"team_id": "t1", "to": "c", "message": "msg3"},
            ]
        )
    t.join(timeout=5)
    msg = str(exc_info.value)
    # Names the input INDEX (1) and the failing RECIPIENT ('b').
    assert "messages[1]" in msg, f"error must name input index 1; got {msg}"
    assert "'b'" in msg, f"error must name failing recipient 'b'; got {msg}"
    assert "boom" in msg, f"error must include the error_text; got {msg}"


def test_send_message_many_rejects_missing_response_in_response(bridge_path):
    """If a row comes back ready but its response_json lacks 'response',
    surface BridgeRemoteError naming the offending input index."""
    wrapper = QueueBridgeWrapper(str(bridge_path), run_id=102)
    _tick_bridge_heartbeat(bridge_path, run_id=102, at_offset_seconds=0)

    def fake_s1():
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            pending = _all_pending_rows(bridge_path, run_id=102)
            if len(pending) >= 2:
                _mark_row_ready(bridge_path, pending[0], {"response": "ok"})
                # row 2 ready but missing the 'response' key entirely.
                _mark_row_ready(bridge_path, pending[1], {"wrong_key": "x"})
                return
            time.sleep(0.01)
        raise AssertionError("never saw 2 pending rows")

    t = threading.Thread(target=fake_s1, daemon=True)
    t.start()
    with pytest.raises(BridgeRemoteError) as exc_info:
        wrapper.send_message_many(
            [
                {"team_id": "t1", "to": "a", "message": "m1"},
                {"team_id": "t1", "to": "b", "message": "m2"},
            ]
        )
    t.join(timeout=5)
    msg = str(exc_info.value)
    assert "messages[1]" in msg
    assert "'b'" in msg


def test_send_message_many_empty_returns_empty(bridge_path):
    """Empty input → empty output, no rows enqueued. Defensive guard."""
    wrapper = QueueBridgeWrapper(str(bridge_path), run_id=103)
    # No heartbeat needed — never polls.
    out = wrapper.send_message_many([])
    assert out == []
    assert _count_rows(bridge_path, run_id=103) == 0


def test_send_message_many_validates_input_shape(bridge_path):
    """Shape errors raise BEFORE anything is enqueued (atomicity-of-validation)."""
    wrapper = QueueBridgeWrapper(str(bridge_path), run_id=104)
    with pytest.raises(ValueError, match="missing required key"):
        wrapper.send_message_many([{"team_id": "t", "to": "a"}])  # message missing
    with pytest.raises(TypeError, match="must be str"):
        wrapper.send_message_many([{"team_id": "t", "to": "a", "message": 123}])
    with pytest.raises(TypeError, match="must be a list"):
        wrapper.send_message_many("not a list")  # type: ignore[arg-type]
    # No rows were enqueued by any failed call.
    assert _count_rows(bridge_path, run_id=104) == 0


# ── GAP-5: CLEANUP_TIMEOUT_S bump (20 → 120) ──────────────────────────────


def test_team_delete_cleanup_deadline_is_120s():
    """GAP-5 (docs/kaizen/2026-05-24-bridge-smoke-2.md): the team_delete
    cleanup deadline is 120s, bumped from the original 20s. Pin both the
    module-level constant and the class-level mirror so any future drift
    fails loudly here rather than silently re-introducing run-22's false-
    failed-marker bug."""
    assert bridge_mod.CLEANUP_TIMEOUT_S == 120.0
    assert QueueBridgeWrapper.CLEANUP_TIMEOUT_S == 120.0


def test_team_delete_does_not_trip_timeout_at_60s_under_new_deadline(bridge_path, monkeypatch):
    """GAP-5 regression: a team_delete row taking ~60s to ready must NOT
    raise BridgeTimeoutError under the new 120s deadline. Under the old
    20s deadline this would have raised (the precise run-22 symptom).

    Mirrors the time-mocking pattern from
    test_long_sendmessage_does_not_trip_stall_at_old_threshold: we
    compress wall clock via a monkeypatched time.monotonic so the test
    finishes fast, but the deadline check (which uses time.monotonic)
    observes a 60s elapse.
    """
    wrapper = QueueBridgeWrapper(str(bridge_path), run_id=200)
    _tick_bridge_heartbeat(bridge_path, run_id=200, at_offset_seconds=0)

    state = {"sim_elapsed": 0.0}
    real_monotonic = time.monotonic

    def fake_monotonic():
        return real_monotonic() + state["sim_elapsed"]

    monkeypatch.setattr(bridge_mod.time, "monotonic", fake_monotonic)

    def fake_s1():
        row_id = _wait_for_pending_row(bridge_path, run_id=200)
        # Refresh heartbeat so the stall predicate doesn't trip — this
        # test is specifically about the per-call deadline, not stall.
        _tick_bridge_heartbeat(bridge_path, run_id=200, at_offset_seconds=0)
        # Advance simulated monotonic to 60s — well past the old 20s
        # deadline, well under the new 120s deadline.
        state["sim_elapsed"] = 60.0
        _mark_row_ready(bridge_path, row_id, {})

    t = threading.Thread(target=fake_s1, daemon=True)
    t.start()
    # Must NOT raise BridgeTimeoutError.
    wrapper.team_delete("team-xyz")
    t.join(timeout=10)
    # Sanity: actually elapsed past the old deadline.
    assert state["sim_elapsed"] >= 60.0


# ── Issue #41: hybrid monotonic-friendly stall check ─────────────────────


def test_hybrid_stall_no_raise_on_wall_clock_skew_alone(bridge_path):
    """Issue #41: laptop suspend/resume defence.

    Simulates the failure mode: SQLite wall clock has jumped FAR forward
    (julianday('now') - bridge_heartbeat.last_polled_at > HEARTBEAT_STALL_S)
    while Python's CLOCK_MONOTONIC has NOT advanced by a comparable amount
    (the macOS suspend behaviour — Python was paused too, so the gap from
    the previous tick to the current tick is small).

    Under the old single-condition stall predicate, this scenario would
    spuriously raise BridgeStallError and abandon the cycle on every
    laptop-resume. The hybrid predicate must NOT raise here — instead it
    falls through to the suspend/resume branch and continues polling.
    """
    # Pre-seed: pretend a PRIOR tick happened recently (so mono_gap measured
    # at the top of the loop is small) by writing _last_python_tick_monotonic
    # to "now-1s". This bypasses the first-iteration None-mono_gap branch
    # and exercises the suspend-resume code path directly.
    run_id = 300
    bridge_mod._last_python_tick_monotonic[run_id] = time.monotonic() - 1.0
    wrapper = QueueBridgeWrapper(str(bridge_path), run_id=run_id)
    # SQLite-side: pretend S1's heartbeat is 8 hours stale (28800s) —
    # WAY past HEARTBEAT_STALL_S=300. This is the wall-clock-skew signal.
    _tick_bridge_heartbeat(bridge_path, run_id=run_id, at_offset_seconds=28800)

    def fake_s1():
        # S1 is alive again — refresh heartbeat to "now" and mark the
        # row ready after the wrapper has had ≥1 poll iteration to
        # observe the suspend signal and reset the deadline.
        row_id = _wait_for_pending_row(bridge_path, run_id=run_id)
        # Give the wrapper one full POLL_INTERVAL_S to observe the skew.
        time.sleep(QueueBridgeWrapper.POLL_INTERVAL_S * 2)
        _tick_bridge_heartbeat(bridge_path, run_id=run_id, at_offset_seconds=0)
        _mark_row_ready(bridge_path, row_id, {"team_id": "post-resume-team"})

    t = threading.Thread(target=fake_s1, daemon=True)
    t.start()
    # Must NOT raise BridgeStallError despite the 8h SQLite-side gap.
    team_id = wrapper.team_create(name="x", members=[])
    t.join(timeout=10)
    assert team_id == "post-resume-team"


def test_hybrid_stall_raises_when_both_clocks_agree(bridge_path):
    """Issue #41 companion: when BOTH the SQLite wall clock gap AND the
    monotonic gap exceed HEARTBEAT_STALL_S, the predicate raises as before.

    This is the "real S1 stall on a machine that was NOT suspended" case —
    Python kept ticking the whole time (mono_gap large), and S1 also went
    silent (julianday gap large). Abandonment is the correct call.
    """
    run_id = 301
    # Pre-seed: pretend a PRIOR tick happened LONG ago (mono_gap will be
    # large). Combined with the stale bridge_heartbeat below, both clocks
    # agree → raise.
    bridge_mod._last_python_tick_monotonic[run_id] = time.monotonic() - 600.0
    wrapper = QueueBridgeWrapper(str(bridge_path), run_id=run_id)
    _tick_bridge_heartbeat(bridge_path, run_id=run_id, at_offset_seconds=600)
    with pytest.raises(BridgeStallError) as exc_info:
        wrapper.team_create(name="x", members=[])
    msg = str(exc_info.value)
    assert "stall" in msg.lower()
    # Both clocks were reported as contributing to the decision.
    assert "python monotonic gap" in msg


def test_python_monotonic_gap_updated_per_tick(bridge_path):
    """Issue #41: ``_tick_python_heartbeat`` must update the module-level
    ``_last_python_tick_monotonic`` entry keyed by run_id on every call —
    that's the load-bearing side-effect the hybrid stall check depends on.
    """
    run_id = 302
    bridge_mod._last_python_tick_monotonic.pop(run_id, None)
    wrapper = QueueBridgeWrapper(str(bridge_path), run_id=run_id)
    assert run_id not in bridge_mod._last_python_tick_monotonic
    wrapper._tick_python_heartbeat()
    first = bridge_mod._last_python_tick_monotonic[run_id]
    assert isinstance(first, float)
    time.sleep(0.05)
    wrapper._tick_python_heartbeat()
    second = bridge_mod._last_python_tick_monotonic[run_id]
    assert second > first, "monotonic tick timestamp must strictly increase"


def test_first_iteration_after_resume_does_not_raise(bridge_path):
    """Issue #41 follow-up (SDET review on the #40-#45 bundled PR).

    The cross-cycle suspend-resume scenario the original #41 fix was filed
    to prevent: a new cycle constructs a FRESH wrapper instance for the
    same run_id after the laptop resumed from suspend. The bridge_heartbeat
    row's `last_polled_at` is hours stale (set pre-suspend), but
    `_last_python_tick_monotonic[run_id]` is empty — this is a fresh
    wrapper instance, no prior tick to measure a monotonic gap against.

    Under the OLD semantics (`mono_gap is None or mono_gap > stall → raise`)
    this fresh-wrapper case would spuriously raise BridgeStallError because
    `mono_gap is None`. Under the FIXED semantics (`mono_gap is not None
    and mono_gap > stall → raise`) it falls through to the suspend-resume
    branch, resets the per-call deadline, and continues polling — which is
    exactly what we want for a fresh resume.

    This test would FAIL on the unfixed (`is None or` short-circuit) code:
    the wrapper would raise BridgeStallError on the first poll iteration
    instead of waiting for S1 to come back. Run this test against the
    pre-fix code to confirm.
    """
    run_id = 303
    # Clear any leftover monotonic tick (defence in depth — fixtures don't
    # currently scrub this module-level dict; see deferred TODO).
    bridge_mod._last_python_tick_monotonic.pop(run_id, None)
    # SQLite-side: heartbeat is 8 hours stale (pre-suspend last_polled_at).
    _tick_bridge_heartbeat(bridge_path, run_id=run_id, at_offset_seconds=8 * 3600)
    # Fresh wrapper instance — emulates "new cycle constructs a new wrapper".
    wrapper = QueueBridgeWrapper(str(bridge_path), run_id=run_id)
    assert run_id not in bridge_mod._last_python_tick_monotonic, (
        "pre-condition: fresh wrapper means no prior monotonic tick"
    )

    # Track that the wrapper actually reached the suspend-resume branch
    # (i.e. it DID observe the stale heartbeat and DID NOT raise). We
    # detect this by: (a) S1 ticks the heartbeat back to fresh on the
    # second iteration, (b) marks the row ready, (c) wrapper returns
    # cleanly. Plus we cross-check the per-call deadline was extended by
    # observing the wrapper waited at least one POLL_INTERVAL_S after the
    # initial stale-heartbeat observation.

    branch_taken = {"observed_stale_then_resumed": False}

    def fake_s1():
        row_id = _wait_for_pending_row(bridge_path, run_id=run_id)
        # Give the wrapper a couple of poll iterations so it DEFINITELY
        # observed the 8h-stale heartbeat at least once. If the wrapper
        # raised, _wait_for_pending_row already succeeded but the main
        # thread is about to raise BridgeStallError before this sleep
        # completes — the test would then fail with the raised exception
        # propagated.
        time.sleep(QueueBridgeWrapper.POLL_INTERVAL_S * 2)
        # S1 came back — refresh heartbeat to now and mark the row ready.
        _tick_bridge_heartbeat(bridge_path, run_id=run_id, at_offset_seconds=0)
        _mark_row_ready(bridge_path, row_id, {"team_id": "post-resume-team"})
        branch_taken["observed_stale_then_resumed"] = True

    t = threading.Thread(target=fake_s1, daemon=True)
    t.start()
    # Must NOT raise BridgeStallError on the first iteration despite the
    # 8h-stale SQLite heartbeat — the fresh wrapper has no monotonic
    # evidence and must default to "treat as suspend-resume, continue".
    team_id = wrapper.team_create(name="x", members=[])
    t.join(timeout=10)
    assert team_id == "post-resume-team"
    assert branch_taken["observed_stale_then_resumed"], (
        "fake_s1 path must have run to completion — confirms wrapper "
        "didn't raise on the first iteration"
    )
    # After the call, the wrapper MUST have ticked python_heartbeat at
    # least once → _last_python_tick_monotonic must now be populated.
    assert run_id in bridge_mod._last_python_tick_monotonic, (
        "wrapper should have ticked at least once before returning"
    )


# ── Issue #42: per-cycle outer wall-clock deadline ───────────────────────


def test_cycle_wall_constant_default():
    """Issue #42: pin the module + class CYCLE_WALL_S default at 3600s."""
    assert bridge_mod.CYCLE_WALL_S == 3600.0
    assert QueueBridgeWrapper.CYCLE_WALL_S == 3600.0


# ── KAIZEN_CYCLE_WALL_S env-override (run-33 operator escape hatch) ─────
#
# Phase-3 mesh agreement caveat C2: defensive parsing — malformed env vars
# MUST NOT abort a cycle. Four-branch coverage below pins the contract.


def test_resolve_cycle_wall_s_default_when_unset(monkeypatch):
    """Branch 1: KAIZEN_CYCLE_WALL_S unset → default 3600.0."""
    monkeypatch.delenv("KAIZEN_CYCLE_WALL_S", raising=False)
    assert bridge_mod._resolve_cycle_wall_s() == 3600.0


def test_resolve_cycle_wall_s_default_when_empty(monkeypatch):
    """Branch 1 (continued): empty string treated identically to unset."""
    monkeypatch.setenv("KAIZEN_CYCLE_WALL_S", "")
    assert bridge_mod._resolve_cycle_wall_s() == 3600.0


def test_resolve_cycle_wall_s_warns_and_defaults_on_non_numeric(monkeypatch, capsys):
    """Branch 2: non-numeric value → stderr warning + default fallback,
    no exception raised (a malformed env var MUST NOT abort a cycle)."""
    monkeypatch.setenv("KAIZEN_CYCLE_WALL_S", "garbage")
    value = bridge_mod._resolve_cycle_wall_s()
    assert value == 3600.0
    captured = capsys.readouterr()
    assert "KAIZEN_CYCLE_WALL_S" in captured.err
    assert "'garbage'" in captured.err
    assert "not numeric" in captured.err


def test_resolve_cycle_wall_s_warns_and_defaults_on_zero(monkeypatch, capsys):
    """Branch 3: numeric but <= 0 → stderr warning + default fallback."""
    monkeypatch.setenv("KAIZEN_CYCLE_WALL_S", "0")
    value = bridge_mod._resolve_cycle_wall_s()
    assert value == 3600.0
    captured = capsys.readouterr()
    assert "KAIZEN_CYCLE_WALL_S" in captured.err
    assert "must be" in captured.err


def test_resolve_cycle_wall_s_warns_and_defaults_on_negative(monkeypatch, capsys):
    """Branch 3 (continued): negative values follow the <= 0 path."""
    monkeypatch.setenv("KAIZEN_CYCLE_WALL_S", "-10")
    value = bridge_mod._resolve_cycle_wall_s()
    assert value == 3600.0
    captured = capsys.readouterr()
    assert "KAIZEN_CYCLE_WALL_S" in captured.err


def test_resolve_cycle_wall_s_uses_positive_value(monkeypatch, capsys):
    """Branch 4: numeric and > 0 → use it, no warning, no upper clamp.

    The operator escape hatch trusts the operator — a 24-hour override
    must round-trip unaltered.
    """
    monkeypatch.setenv("KAIZEN_CYCLE_WALL_S", "7200")
    assert bridge_mod._resolve_cycle_wall_s() == 7200.0

    monkeypatch.setenv("KAIZEN_CYCLE_WALL_S", "86400")
    assert bridge_mod._resolve_cycle_wall_s() == 86400.0

    monkeypatch.setenv("KAIZEN_CYCLE_WALL_S", "0.5")
    assert bridge_mod._resolve_cycle_wall_s() == 0.5

    captured = capsys.readouterr()
    assert captured.err == "", (
        f"positive values must not emit a warning; got stderr: {captured.err!r}"
    )


def test_cycle_wall_clock_exceeded_raises_bridge_stall_error(bridge_path, monkeypatch):
    """Issue #42: when the per-cycle outer wall-clock budget is exceeded,
    the next ``_request()`` call raises ``BridgeStallError`` carrying
    'cycle wall-clock exceeded' in the message.

    Drives the deadline trip via a short CYCLE_WALL_S override (10s) and a
    monkeypatched ``time.monotonic`` that fast-forwards past it. The fake
    S1 keeps heartbeating so the hybrid stall predicate doesn't fire first.
    """
    run_id = 400
    wrapper = QueueBridgeWrapper(str(bridge_path), run_id=run_id)
    # Tighten the budget so the test trips it without simulating an hour.
    wrapper.CYCLE_WALL_S = 10.0
    _tick_bridge_heartbeat(bridge_path, run_id=run_id, at_offset_seconds=0)

    # Compress wall clock: every monotonic call advances by 5s of simulated
    # time. After ~2-3 polls the wrapper sees self._cycle_deadline blown
    # and raises. We keep S1 heartbeating (refreshed inside fake_s1 below)
    # to ensure the cycle-wall raise — not the stall raise — fires.
    state = {"sim_elapsed": 0.0}
    real_monotonic = time.monotonic

    def fake_monotonic():
        # Advance by 5s of simulated time on every call; this aggregates
        # past the 10s cycle budget quickly.
        state["sim_elapsed"] += 5.0
        return real_monotonic() + state["sim_elapsed"]

    monkeypatch.setattr(bridge_mod.time, "monotonic", fake_monotonic)

    def fake_s1():
        # Refresh the heartbeat repeatedly so the stall predicate never trips.
        for _ in range(20):
            _tick_bridge_heartbeat(bridge_path, run_id=run_id, at_offset_seconds=0)
            time.sleep(0.02)

    t = threading.Thread(target=fake_s1, daemon=True)
    t.start()
    with pytest.raises(BridgeStallError) as exc_info:
        wrapper.team_create(name="x", members=[])
    t.join(timeout=5)
    msg = str(exc_info.value)
    assert "cycle wall-clock exceeded" in msg, (
        f"expected 'cycle wall-clock exceeded' in error; got: {msg}"
    )
    assert "CYCLE_WALL_S=10.0" in msg


def test_cycle_deadline_lazy_init_on_first_request(bridge_path):
    """Issue #42: ``_cycle_deadline`` is None at construction and is set
    on the first ``_request()`` call. Pre-call inspection must see None;
    a successful call must leave it populated."""
    wrapper = QueueBridgeWrapper(str(bridge_path), run_id=401)
    assert wrapper._cycle_deadline is None
    _tick_bridge_heartbeat(bridge_path, run_id=401, at_offset_seconds=0)

    def fake_s1():
        row_id = _wait_for_pending_row(bridge_path, run_id=401)
        _mark_row_ready(bridge_path, row_id, {"team_id": "t"})

    t = threading.Thread(target=fake_s1, daemon=True)
    t.start()
    wrapper.team_create(name="x", members=[])
    t.join(timeout=5)
    assert wrapper._cycle_deadline is not None
    assert isinstance(wrapper._cycle_deadline, float)


def test_reset_cycle_deadline_clears_state(bridge_path):
    """Issue #42: ``reset_cycle_deadline()`` clears the deadline so a
    fresh ``CYCLE_WALL_S`` budget starts on the next ``_request()`` call.
    This is the explicit-reset hook for callers that reuse a wrapper
    across cycles (the production path uses one wrapper per cycle, so this
    method is the defensive escape hatch)."""
    wrapper = QueueBridgeWrapper(str(bridge_path), run_id=402)
    _tick_bridge_heartbeat(bridge_path, run_id=402, at_offset_seconds=0)

    def fake_s1_once():
        row_id = _wait_for_pending_row(bridge_path, run_id=402)
        _mark_row_ready(bridge_path, row_id, {"team_id": "t1"})

    t = threading.Thread(target=fake_s1_once, daemon=True)
    t.start()
    wrapper.team_create(name="x", members=[])
    t.join(timeout=5)
    assert wrapper._cycle_deadline is not None
    wrapper.reset_cycle_deadline()
    assert wrapper._cycle_deadline is None

    # Second call starts a fresh deadline.
    def fake_s1_twice():
        row_id = _wait_for_pending_row(bridge_path, run_id=402)
        _mark_row_ready(bridge_path, row_id, {"team_id": "t2"})

    t = threading.Thread(target=fake_s1_twice, daemon=True)
    t.start()
    wrapper.team_create(name="y", members=[])
    t.join(timeout=5)
    assert wrapper._cycle_deadline is not None
