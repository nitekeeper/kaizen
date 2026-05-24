"""Queue-bridge `AgentTeamsWrapper` subclass — Python ↔ Claude Code session.

`QueueBridgeWrapper` is the production wrapper that ships `TeamCreate`,
`SendMessage`, `TeamDelete` invocations from the detached Python process
(running `orchestrate_run`) across to the orchestrating Claude session
(S1) via a SQLite queue in `.ai/bridge.db`.

Per the python-cc-tool-bridge design (Rev 4):

  * Each tool call is enqueued as a `bridge_requests` row with
    `status='pending'`. Python then polls the row, using `time.monotonic()`
    (NOT `datetime.now()`) so laptop sleep doesn't artificially trip
    the per-call timeout.

  * `python_heartbeat` is UPSERTED on EVERY poll tick. S1 reads it to
    distinguish "Python crashed" from "Python is slow."

  * If `bridge_heartbeat.last_polled_at` is older than
    `HEARTBEAT_STALL_S` seconds, S1 has stopped polling — raise
    `BridgeStallError` immediately rather than waiting out the
    per-call timeout.

  * If the row comes back with `status='error'`, raise
    `BridgeRemoteError` carrying the recorded `error_text`.

  * `queue_bridge_provider(db_path, run_id)` returns the
    `tools_provider` callable slot expected by
    `orchestrate_run(tools_provider=...)`.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from scripts.bridge_db import bootstrap
from scripts.team_tools_wrapper import AgentTeamsWrapper

# Per the design's "Code-level API sketch (Rev 4)" section.
#
# Run-21 bump (2026-05-24): HEARTBEAT_STALL_S raised 60→300 and
# PER_CALL_TIMEOUT_S raised 180→600 in response to GAP-1 from
# docs/kaizen/2026-05-24-bridge-smoke.md. The Rev-4 design assumed
# the per-row heartbeat poke (Appendix A step 2a) would bound the
# heartbeat gap to one Bash latency — true for the synchronous
# TeamCreate / TeamDelete tools, FALSE for CC team-mode SendMessage,
# which is fundamentally async: the orchestrating Claude (S1) is idle
# while waiting for the teammate's reply notification, so no Bash
# heartbeat fires for the full 30-60+s round-trip the smoke observed.
# Bumping the threshold trades a longer "real crash invisible" window
# (300s) for elimination of spurious abandonment on legitimate replies.
# Acceptable for the personal-use single-machine deployment context.
# Trade-off pinned in the smoke report's GAP-1 follow-up note.
PER_CALL_TIMEOUT_S: float = 600.0
# Cleanup is best-effort. Longer is safer than racing the orchestrator's
# turn-cycle latency (Bash heartbeat + TeamDelete tool call + bridge_write
# spans multiple Claude turns; each turn is ~5-10s). Run 22 smoke saw the
# 20s default trip BridgeTimeoutError even though the team was successfully
# cleaned up — Python just didn't observe in time. See docs/kaizen/2026-05-24-bridge-smoke-2.md GAP-5.
CLEANUP_TIMEOUT_S: float = 120.0
HEARTBEAT_STALL_S: float = 300.0
POLL_INTERVAL_S: float = 0.2
STALE_ROW_S: float = 900.0


class BridgeError(RuntimeError):
    """Base class for queue-bridge failures observable by Python."""


class BridgeStallError(BridgeError):
    """Raised when `bridge_heartbeat.last_polled_at` is older than
    `HEARTBEAT_STALL_S` — S1 has stopped polling."""


class BridgeRemoteError(BridgeError):
    """Raised when a queue row comes back as `status='error'` — the
    Claude session attempted the session-tool call but it failed."""


class BridgeTimeoutError(BridgeError):
    """Raised when a queue row stays `status='pending'` beyond the
    per-call timeout even though S1 is still heartbeating."""


def _connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a bridge-DB connection with the standard PRAGMAs applied."""
    con = sqlite3.connect(str(db_path))
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA busy_timeout = 5000;")
    return con


class QueueBridgeWrapper(AgentTeamsWrapper):
    """Production wrapper. INSERTs requests; polls for status='ready'.

    See module docstring for the full contract. Constructed once per
    cycle by :func:`queue_bridge_provider`.
    """

    PER_CALL_TIMEOUT_S: float = PER_CALL_TIMEOUT_S
    CLEANUP_TIMEOUT_S: float = CLEANUP_TIMEOUT_S
    HEARTBEAT_STALL_S: float = HEARTBEAT_STALL_S
    POLL_INTERVAL_S: float = POLL_INTERVAL_S
    STALE_ROW_S: float = STALE_ROW_S

    def __init__(self, db_path: str | Path, run_id: int):
        self._db_path = Path(db_path)
        self._run_id = int(run_id)
        # Last-line guard — if a user wired this up bare without going
        # through run_bridged.py, the schema may not exist yet.
        bootstrap(self._db_path)

    # ── public AgentTeamsWrapper overrides ────────────────────────────

    def team_create(self, name: str, members: list[str]) -> str:
        resp = self._request(
            "team_create",
            {"name": name, "members": list(members)},
        )
        team_id = resp.get("team_id")
        if not isinstance(team_id, str) or not team_id:
            raise BridgeRemoteError(f"team_create response missing 'team_id' string: {resp!r}")
        return team_id

    def send_message(self, team_id: str, to: str, message: str) -> str:
        resp = self._request(
            "send_message",
            {"team_id": team_id, "to": to, "message": message},
        )
        out = resp.get("response")
        if not isinstance(out, str):
            raise BridgeRemoteError(f"send_message response missing 'response' string: {resp!r}")
        return out

    def send_message_many(self, messages: list[dict]) -> list[str]:
        """Batch variant of send_message — enqueue N rows in one transaction
        and wait for ALL to reach status='ready' before returning the
        responses in INPUT ORDER.

        Each dict in ``messages`` must have keys ``team_id`` (str), ``to``
        (str), ``message`` (str). Returns a list[str] of
        ``response_json["response"]`` strings, in the same order as input
        ``messages``.

        Raises:
          * BridgeRemoteError — if any row reaches status='error' OR if any
            row's response is missing the 'response' string. The error
            message names the failing input index and recipient.
          * BridgeStallError — if S1's heartbeat goes older than
            HEARTBEAT_STALL_S at any point during the batch wait.
          * BridgeTimeoutError — if the whole batch fails to complete
            within PER_CALL_TIMEOUT_S (applied to the slowest row).

        See ``send_message`` for the singular dispatch; this method is the
        Phase 2 / Phase 3 Star-open / Phase 5b' fan-out optimisation that
        GAP-4 of docs/kaizen/2026-05-24-bridge-smoke-2.md surfaced — N
        parallel dispatches in one Python-side blocking call rather than
        N sequential round-trips.
        """
        if not isinstance(messages, list):
            raise TypeError(
                f"send_message_many: 'messages' must be a list, got {type(messages).__name__}"
            )
        if not messages:
            return []
        # Validate shape up-front so we never enqueue a half-bad batch.
        for idx, m in enumerate(messages):
            if not isinstance(m, dict):
                raise TypeError(
                    f"send_message_many: messages[{idx}] must be a dict, got {type(m).__name__}"
                )
            for key in ("team_id", "to", "message"):
                if key not in m:
                    raise ValueError(
                        f"send_message_many: messages[{idx}] missing required key {key!r}"
                    )
                if not isinstance(m[key], str):
                    raise TypeError(
                        f"send_message_many: messages[{idx}][{key!r}] must be str, "
                        f"got {type(m[key]).__name__}"
                    )

        row_ids = self._insert_many("send_message", messages)
        responses = self._poll_many(row_ids, kind="send_message", messages=messages)
        # `responses` is keyed by row_id in our input order; unwrap to
        # response strings and validate each.
        out: list[str] = []
        for idx, resp in enumerate(responses):
            response_str = resp.get("response") if isinstance(resp, dict) else None
            if not isinstance(response_str, str):
                recipient = messages[idx]["to"]
                raise BridgeRemoteError(
                    f"send_message_many: messages[{idx}] (to={recipient!r}) "
                    f"response missing 'response' string: {resp!r}"
                )
            out.append(response_str)
        return out

    def team_delete(self, team_id: str) -> None:
        # team_delete's response_json is `{}`. Cleanup deadline is the
        # bumped CLEANUP_TIMEOUT_S=120s (see the GAP-5 rationale comment
        # above the module-level constant) — best-effort, larger than the
        # orchestrator's turn-cycle latency.
        self._request(
            "team_delete",
            {"team_id": team_id},
            timeout_s=self.CLEANUP_TIMEOUT_S,
        )

    # ── internal helpers ──────────────────────────────────────────────

    def _insert(self, kind: str, args: dict) -> int:
        """INSERT a pending bridge_requests row. Returns the new id."""
        con = _connect(self._db_path)
        try:
            cur = con.execute(
                "INSERT INTO bridge_requests (run_id, kind, args_json, status) "
                "VALUES (?, ?, ?, 'pending')",
                (self._run_id, kind, json.dumps(args)),
            )
            con.commit()
            return int(cur.lastrowid)
        finally:
            con.close()

    def _insert_many(self, kind: str, items: list[dict]) -> list[int]:
        """INSERT N pending bridge_requests rows in a single transaction.
        Returns row ids in input order. The single-transaction property is
        load-bearing — partial-batch enqueue would corrupt ordering and
        violate the all-or-nothing batch contract."""
        con = _connect(self._db_path)
        try:
            con.execute("BEGIN")
            row_ids: list[int] = []
            for args in items:
                cur = con.execute(
                    "INSERT INTO bridge_requests (run_id, kind, args_json, status) "
                    "VALUES (?, ?, ?, 'pending')",
                    (self._run_id, kind, json.dumps(args)),
                )
                row_ids.append(int(cur.lastrowid))
            con.commit()
            return row_ids
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def _poll_many(
        self,
        row_ids: list[int],
        *,
        kind: str,
        messages: list[dict],
    ) -> list[dict]:
        """Poll N bridge_requests rows in lock-step. Returns the decoded
        ``response_json`` dicts in INPUT ORDER (aligned to ``row_ids``).

        Per-batch deadline budget = ``PER_CALL_TIMEOUT_S`` (applied to the
        SLOWEST row — same semantics as ``_request`` but scoped to the
        whole batch). Per-batch stall budget = ``HEARTBEAT_STALL_S``.

        Mirrors the single-row poll loop (``_request``) — same stall and
        timeout invariants, just SELECT'ing N rows at once.
        """
        # Map row_id → (input_index, recipient) for error attribution
        id_to_index = {rid: i for i, rid in enumerate(row_ids)}
        id_to_recipient = {rid: messages[i].get("to", "?") for i, rid in enumerate(row_ids)}
        # Placeholder list ordered by input index.
        results: list[dict | None] = [None] * len(row_ids)
        completed: set[int] = set()
        placeholders = ",".join("?" for _ in row_ids)
        deadline = time.monotonic() + self.PER_CALL_TIMEOUT_S
        while True:
            self._tick_python_heartbeat()
            con = _connect(self._db_path)
            try:
                # nosec B608 — `placeholders` is built only from literal "?" chars
                # (line 276); row_id values are passed via parameter binding as the
                # `tuple(row_ids)` second arg. Same defensive pattern as scripts/pr.py:80,
                # scripts/bridge_write.py:100, scripts/project.py:155.
                cur = con.execute(
                    f"SELECT id, status, response_json, error_text "
                    f"FROM bridge_requests WHERE id IN ({placeholders}) ORDER BY id",  # nosec B608
                    tuple(row_ids),
                )
                rows = cur.fetchall()
            finally:
                con.close()
            seen_ids = {row[0] for row in rows}
            # Any disappeared row → treat as error attributed to that input.
            for rid in row_ids:
                if rid not in seen_ids and rid not in completed:
                    idx = id_to_index[rid]
                    recipient = id_to_recipient[rid]
                    raise BridgeRemoteError(
                        f"send_message_many: messages[{idx}] (to={recipient!r}) "
                        f"row {rid} disappeared from queue"
                    )
            for rid, status, response_json, error_text in rows:
                if rid in completed:
                    continue
                if status == "ready":
                    idx = id_to_index[rid]
                    if not response_json:
                        results[idx] = {}
                    else:
                        try:
                            results[idx] = json.loads(response_json)
                        except json.JSONDecodeError as e:
                            recipient = id_to_recipient[rid]
                            raise BridgeRemoteError(
                                f"send_message_many: messages[{idx}] (to={recipient!r}) "
                                f"row {rid} ({kind}) response_json is not valid JSON: {e}"
                            ) from e
                    completed.add(rid)
                elif status == "error":
                    idx = id_to_index[rid]
                    recipient = id_to_recipient[rid]
                    raise BridgeRemoteError(
                        f"send_message_many: messages[{idx}] (to={recipient!r}) "
                        f"row {rid} ({kind}) failed: {error_text or '(no error_text)'}"
                    )
                # else status == 'pending' — keep waiting

            if len(completed) == len(row_ids):
                # All done — results list is fully populated.
                return [r if r is not None else {} for r in results]

            # Stall + deadline checks — same shape as _request, just batch-scoped.
            stall = self._s1_seconds_since_last_poll()
            if stall is not None and stall > self.HEARTBEAT_STALL_S:
                pending_ids = [rid for rid in row_ids if rid not in completed]
                raise BridgeStallError(
                    f"S1 heartbeat stalled during send_message_many batch: "
                    f"last_polled_at is {stall:.1f}s old "
                    f"(> HEARTBEAT_STALL_S={self.HEARTBEAT_STALL_S}s); "
                    f"{len(pending_ids)} of {len(row_ids)} rows still pending "
                    f"(rows={pending_ids})"
                )

            if time.monotonic() >= deadline:
                pending_ids = [rid for rid in row_ids if rid not in completed]
                raise BridgeTimeoutError(
                    f"send_message_many: {len(pending_ids)} of {len(row_ids)} rows "
                    f"timed out after {self.PER_CALL_TIMEOUT_S}s "
                    f"(S1 heartbeat alive, but rows never reached 'ready'); "
                    f"pending rows={pending_ids}"
                )

            time.sleep(self.POLL_INTERVAL_S)

    def _poll(self, row_id: int) -> tuple[str, str | None, str | None]:
        """Return (status, response_json, error_text) for the row."""
        con = _connect(self._db_path)
        try:
            cur = con.execute(
                "SELECT status, response_json, error_text FROM bridge_requests WHERE id = ?",
                (row_id,),
            )
            row = cur.fetchone()
            if row is None:
                # The row vanished — treat as a remote error.
                return ("error", None, f"row {row_id} disappeared from queue")
            return (row[0], row[1], row[2])
        finally:
            con.close()

    def _tick_python_heartbeat(self) -> None:
        """UPSERT `python_heartbeat` for this run. Called on EVERY poll
        tick so S1 can prove Python is alive even when the row Python is
        waiting on takes a long time to come back."""
        con = _connect(self._db_path)
        try:
            con.execute(
                "INSERT INTO python_heartbeat (run_id, last_beat_at, beat_count) "
                "VALUES (?, datetime('now'), 1) "
                "ON CONFLICT(run_id) DO UPDATE SET "
                "last_beat_at = datetime('now'), "
                "beat_count = beat_count + 1",
                (self._run_id,),
            )
            con.commit()
        finally:
            con.close()

    def _s1_seconds_since_last_poll(self) -> float | None:
        """Return wall-clock seconds since S1's last bridge_heartbeat
        tick (or None if S1 has not yet ticked once). Uses julianday()
        for robustness (MINOR-PYTHON-HB-CHECK).

        m7 (review round 1): the None return is asymmetric on purpose
        — caller _request() treats None as "S1 still booting, assume
        alive" and falls through to the per-call timeout check. A
        present-and-stale heartbeat (row[0] > HEARTBEAT_STALL_S) is
        the only condition that trips BridgeStallError. Rationale:
        on a cold start S1 fires its first bridge_heartbeat UPSERT
        inside the FIRST iteration of the poll loop — there is a
        small window before that first tick where the row simply
        doesn't exist; raising BridgeStallError then would abandon
        every cycle on its first request.

        # TODO(follow-up): wall-clock skew on laptop suspend can make
        # julianday('now') jump 8h forward while monotonic stays still
        # — would trip a spurious stall. Out of scope here per the
        # round-1 review DEFER decision (m4); revisit once we have a
        # cross-platform monotonic-friendly stall check.
        """
        con = _connect(self._db_path)
        try:
            cur = con.execute(
                "SELECT (julianday('now') - julianday(last_polled_at)) * 86400 "
                "FROM bridge_heartbeat WHERE run_id = ?",
                (self._run_id,),
            )
            row = cur.fetchone()
            # No row OR NULL value → "S1 hasn't ticked yet": assume
            # alive (still booting). Present-and-stale → abandon.
            if row is None or row[0] is None:
                return None
            return float(row[0])
        finally:
            con.close()

    def _request(
        self,
        kind: str,
        args: dict,
        *,
        timeout_s: float | None = None,
    ) -> dict:
        """Enqueue + poll one bridge_requests row. Returns the decoded
        `response_json` dict (or raises one of the Bridge*Error)."""
        # TODO(follow-up): per-cycle outer deadline — see review round-1
        # architect finding. Worst case today is PER_CALL_TIMEOUT_S * (number
        # of dispatches per cycle), so a 50-call cycle could in principle
        # block 8h+ before any single-call timeout fires. A `CYCLE_WALL_S`
        # bound (≈3600s) would cap that. Out of scope for run 21; defer.
        timeout_s = self.PER_CALL_TIMEOUT_S if timeout_s is None else timeout_s
        row_id = self._insert(kind, args)
        deadline = time.monotonic() + timeout_s
        while True:
            self._tick_python_heartbeat()
            status, response_json, error_text = self._poll(row_id)

            if status == "ready":
                if not response_json:
                    return {}
                try:
                    return json.loads(response_json)
                except json.JSONDecodeError as e:
                    raise BridgeRemoteError(
                        f"row {row_id} ({kind}) response_json is not valid JSON: {e}"
                    ) from e

            if status == "error":
                raise BridgeRemoteError(
                    f"row {row_id} ({kind}) failed: {error_text or '(no error_text)'}"
                )

            # status == 'pending': decide whether to keep waiting.
            stall = self._s1_seconds_since_last_poll()
            # When S1 has heartbeated at least once, a stall older than
            # HEARTBEAT_STALL_S means S1 is gone — raise immediately
            # rather than waiting out the full per-call timeout.
            if stall is not None and stall > self.HEARTBEAT_STALL_S:
                raise BridgeStallError(
                    f"S1 heartbeat stalled: last_polled_at is "
                    f"{stall:.1f}s old (> HEARTBEAT_STALL_S={self.HEARTBEAT_STALL_S}s); "
                    f"row {row_id} ({kind}) abandoned"
                )

            if time.monotonic() >= deadline:
                raise BridgeTimeoutError(
                    f"row {row_id} ({kind}) timed out after {timeout_s}s "
                    "(S1 heartbeat alive, but row never reached 'ready')"
                )

            time.sleep(self.POLL_INTERVAL_S)


def queue_bridge_provider(
    db_path: str | Path,
    run_id: int,
) -> Callable:
    """Return a `tools_provider` callable suitable for
    `orchestrate_run(tools_provider=...)`.

    The returned callable ignores `(clone_dir, project, run_row, cycle_n)`
    — the wrapper is fully parameterised by `(db_path, run_id)` and
    re-using the same instance across cycles is correct because the
    queue is keyed on `run_id`, not cycle number.
    """
    bridge_db_path = Path(db_path)

    def _provider(clone_dir, project, run_row, cycle_n):
        # All four positional args are part of the tools_provider
        # contract but unused here — the wrapper is fully parameterised
        # by (db_path, run_id) closed over above.
        del clone_dir, project, run_row, cycle_n
        return QueueBridgeWrapper(bridge_db_path, run_id)

    return _provider
