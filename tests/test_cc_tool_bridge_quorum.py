"""Tests for the per-phase quorum + soft-timeout path in QueueBridgeWrapper (#83).

Real-bridge-db contract tests (no invented mocks, per kaizen
'mocks-must-match-reality'): every test bootstraps a temp ``bridge.db`` and
drives genuine ``bridge_requests`` rows through the real status machine.

Covers:
  * quorum met with a silent straggler → soft-dropped in INPUT ORDER, DB row
    stays 'pending' (the orchestrator owns row lifecycle).
  * quorum NOT met → the hard backstop still raises (no premature drop).
  * a 'error' row inside an otherwise-quorum-met batch still raises HARD
    (quorum forgives silence, never failure).
  * grace-after-quorum: a straggler that answers within its soft-timeout is
    NOT dropped — it is accepted as a genuine reply.
  * strict default (quorum_floor=None) preserves the all-N contract.
  * team_delete bypasses the cycle-wall (cleanup=True) and is best-effort.
  * the ceil(0.75*N) quorum_for() edge table, incl. the intentional small-N
    'no forgiveness' behaviour (N=2 with one straggler still raises).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest

from scripts.bridge_db import bootstrap
from scripts.bridge_softdrop import SOFT_DROP_SENTINEL, is_soft_drop_record
from scripts.cc_tool_bridge import (
    BridgeRemoteError,
    BridgeTimeoutError,
    QueueBridgeWrapper,
    quorum_for,
)


@pytest.fixture
def bridge_path(tmp_path):
    p = tmp_path / ".ai" / "bridge.db"
    bootstrap(str(p))
    return p


def _con(bridge_path):
    return sqlite3.connect(str(bridge_path))


def _fresh_heartbeat(bridge_path, run_id):
    con = _con(bridge_path)
    try:
        con.execute(
            "INSERT INTO bridge_heartbeat (run_id, last_polled_at, polled_count) "
            "VALUES (?, datetime('now'), 1) "
            "ON CONFLICT(run_id) DO UPDATE SET last_polled_at = datetime('now')",
            (run_id,),
        )
        con.commit()
    finally:
        con.close()


def _mark_ready(bridge_path, row_id, response: dict):
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


def _mark_error(bridge_path, row_id, error_text: str):
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


def _row_status(bridge_path, row_id) -> str:
    con = _con(bridge_path)
    try:
        cur = con.execute("SELECT status FROM bridge_requests WHERE id = ?", (row_id,))
        return cur.fetchone()[0]
    finally:
        con.close()


def _msgs(n: int) -> list[dict]:
    return [{"team_id": "t", "to": f"agent-{i}", "message": f"m{i}"} for i in range(n)]


def _fast_wrapper(bridge_path, run_id, **overrides) -> QueueBridgeWrapper:
    """Wrapper tuned for sub-second tests. Soft-timeout small so the quorum
    drop fires quickly; hard backstops large so only the path under test
    governs (unless a test overrides them)."""
    w = QueueBridgeWrapper(str(bridge_path), run_id=run_id)
    w.POLL_INTERVAL_S = 0.02
    w.ROW_SOFT_TIMEOUT_S = 0.3
    w.PER_CALL_TIMEOUT_S = 30.0
    w.HEARTBEAT_STALL_S = 300.0
    w.CYCLE_WALL_S = 300.0
    for k, v in overrides.items():
        setattr(w, k, v)
    return w


# ── quorum behaviour ──────────────────────────────────────────────────────


def test_quorum_met_with_straggler_soft_dropped(bridge_path):
    """4 rows, quorum=3: mark 3 ready, leave 1 pending. After the soft-timeout
    the straggler is soft-dropped in its input slot and the DB row stays
    'pending' (no synthetic 'ready' write)."""
    run_id = 101
    _fresh_heartbeat(bridge_path, run_id)
    w = _fast_wrapper(bridge_path, run_id)
    msgs = _msgs(4)
    row_ids = w._insert_many("send_message", msgs)
    # Genuine replies for rows 0,1,2; row 3 stays silent (pending).
    for i in (0, 1, 2):
        _mark_ready(bridge_path, row_ids[i], {"response": f"reply-{i}"})

    results = w._poll_many(row_ids, kind="send_message", messages=msgs, quorum_floor=quorum_for(4))

    assert len(results) == 4
    for i in (0, 1, 2):
        assert results[i] == {"response": f"reply-{i}"}
        assert not is_soft_drop_record(results[i])
    # Straggler slot is the synthetic soft-drop record, in INPUT ORDER.
    assert is_soft_drop_record(results[3])
    assert results[3]["to"] == "agent-3"
    assert results[3]["response"].startswith(SOFT_DROP_SENTINEL)
    # Data-integrity: the soft-dropped row is NOT written to 'ready' in the DB.
    assert _row_status(bridge_path, row_ids[3]) == "pending"


def test_quorum_not_met_hits_hard_backstop(bridge_path):
    """quorum=3 but only 1 row ever ready → quorum never met, so the per-call
    hard deadline (not soft-drop) governs and raises."""
    run_id = 102
    _fresh_heartbeat(bridge_path, run_id)
    w = _fast_wrapper(bridge_path, run_id, PER_CALL_TIMEOUT_S=0.4)
    msgs = _msgs(4)
    row_ids = w._insert_many("send_message", msgs)
    _mark_ready(bridge_path, row_ids[0], {"response": "only-one"})

    with pytest.raises(BridgeTimeoutError):
        w._poll_many(row_ids, kind="send_message", messages=msgs, quorum_floor=quorum_for(4))


def test_error_row_raises_even_when_quorum_met(bridge_path):
    """quorum=3, 3 rows ready but the 4th is 'error' → must raise
    BridgeRemoteError. Quorum forgives silence, never failure."""
    run_id = 103
    _fresh_heartbeat(bridge_path, run_id)
    w = _fast_wrapper(bridge_path, run_id)
    msgs = _msgs(4)
    row_ids = w._insert_many("send_message", msgs)
    for i in (0, 1, 2):
        _mark_ready(bridge_path, row_ids[i], {"response": f"reply-{i}"})
    _mark_error(bridge_path, row_ids[3], "teammate crashed")

    with pytest.raises(BridgeRemoteError):
        w._poll_many(row_ids, kind="send_message", messages=msgs, quorum_floor=quorum_for(4))


def test_grace_after_quorum_straggler_answers_not_dropped(bridge_path):
    """Quorum met but the straggler answers WITHIN its soft-timeout → it is
    accepted as a genuine reply, never soft-dropped."""
    run_id = 104
    _fresh_heartbeat(bridge_path, run_id)
    # Large soft-timeout so the grace window is wide.
    w = _fast_wrapper(bridge_path, run_id, ROW_SOFT_TIMEOUT_S=5.0)
    msgs = _msgs(4)
    row_ids = w._insert_many("send_message", msgs)
    for i in (0, 1, 2):
        _mark_ready(bridge_path, row_ids[i], {"response": f"reply-{i}"})

    def late_reply():
        time.sleep(0.2)  # well inside the 5s grace window
        _mark_ready(bridge_path, row_ids[3], {"response": "late-but-real"})

    threading.Thread(target=late_reply, daemon=True).start()
    results = w._poll_many(row_ids, kind="send_message", messages=msgs, quorum_floor=quorum_for(4))

    assert results[3] == {"response": "late-but-real"}
    assert not any(is_soft_drop_record(r) for r in results)


def test_strict_default_preserves_all_n(bridge_path):
    """quorum_floor=None (the default) → strict: the batch only returns once
    EVERY row is genuinely ready, even though a quorum was reached earlier."""
    run_id = 105
    _fresh_heartbeat(bridge_path, run_id)
    w = _fast_wrapper(bridge_path, run_id)
    msgs = _msgs(4)
    row_ids = w._insert_many("send_message", msgs)
    for i in (0, 1, 2):
        _mark_ready(bridge_path, row_ids[i], {"response": f"reply-{i}"})

    def finish_last():
        time.sleep(0.5)  # > ROW_SOFT_TIMEOUT_S: proves no soft-drop in strict mode
        _mark_ready(bridge_path, row_ids[3], {"response": "reply-3"})

    threading.Thread(target=finish_last, daemon=True).start()
    results = w._poll_many(row_ids, kind="send_message", messages=msgs)  # quorum_floor=None

    assert [r["response"] for r in results] == [f"reply-{i}" for i in range(4)]
    assert not any(is_soft_drop_record(r) for r in results)


def test_small_n_two_with_straggler_still_raises(bridge_path):
    """quorum_for(2)==2 → a 2-row batch forgives nothing. One ready + one
    silent never meets quorum, so the hard backstop raises (intentional: a
    small evidence base has no redundancy to spare)."""
    assert quorum_for(2) == 2
    run_id = 106
    _fresh_heartbeat(bridge_path, run_id)
    w = _fast_wrapper(bridge_path, run_id, PER_CALL_TIMEOUT_S=0.4)
    msgs = _msgs(2)
    row_ids = w._insert_many("send_message", msgs)
    _mark_ready(bridge_path, row_ids[0], {"response": "one"})

    with pytest.raises(BridgeTimeoutError):
        w._poll_many(row_ids, kind="send_message", messages=msgs, quorum_floor=quorum_for(2))


# ── quorum_for edge table ─────────────────────────────────────────────────


def test_quorum_for_edge_table():
    # ceil(0.75*N), max(1, ...). Forgives nothing for N<=3; 1 straggler at N=4.
    assert quorum_for(1) == 1
    assert quorum_for(2) == 2
    assert quorum_for(3) == 3
    assert quorum_for(4) == 3
    assert quorum_for(5) == 4
    assert quorum_for(8) == 6


# ── team_delete cleanup bypass + best-effort ──────────────────────────────


def test_team_delete_bypasses_expired_cycle_wall(bridge_path):
    """team_delete must complete even when the cycle wall already expired
    (cleanup=True bypass). Without it, teardown would raise BridgeStallError
    on the first poll and leak the team."""
    run_id = 107
    _fresh_heartbeat(bridge_path, run_id)
    w = _fast_wrapper(bridge_path, run_id)
    # Pretend the cycle wall blew long ago (the case that needs teardown).
    w._cycle_deadline = time.monotonic() - 1.0

    def service():
        # Mark the team_delete row ready shortly after it is enqueued.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            con = _con(bridge_path)
            try:
                cur = con.execute(
                    "SELECT id FROM bridge_requests WHERE run_id=? AND kind='team_delete' "
                    "AND status='pending' ORDER BY id LIMIT 1",
                    (run_id,),
                )
                row = cur.fetchone()
            finally:
                con.close()
            if row is not None:
                _mark_ready(bridge_path, int(row[0]), {})
                return
            time.sleep(0.01)

    threading.Thread(target=service, daemon=True).start()
    # Must NOT raise despite the expired cycle deadline.
    assert w.team_delete("doomed-team") is None


def test_team_delete_is_best_effort_on_timeout(bridge_path):
    """A teardown that never completes must be swallowed (best-effort), not
    raised — a teardown that can raise is not a teardown."""
    run_id = 108
    _fresh_heartbeat(bridge_path, run_id)
    # Tiny cleanup timeout, nobody services the row → it would time out.
    w = _fast_wrapper(bridge_path, run_id, CLEANUP_TIMEOUT_S=0.3)
    # No servicing thread: the team_delete row stays pending forever.
    assert w.team_delete("never-reaped") is None
