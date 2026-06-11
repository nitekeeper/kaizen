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
    `HEARTBEAT_STALL_S` seconds, S1 may have stopped polling. The raise
    is gated two ways (Issue #41 suspend/resume tolerance): fire
    immediately when the local monotonic clock corroborates the gap
    (both clocks agree → real stall), OR fire after the stale
    `last_polled_at` value has persisted UNCHANGED through a
    `STALL_CONFIRM_S` confirmation window of continuous Python ticking
    (a dead S1 never refreshes the value; a resumed S1 refreshes it
    within seconds). Either way the call raises `BridgeStallError`
    rather than waiting out the per-call timeout.

  * If the row comes back with `status='error'`, raise
    `BridgeRemoteError` carrying the recorded `error_text`.

  * `queue_bridge_provider(db_path, run_id)` returns the
    `tools_provider` callable slot expected by
    `orchestrate_run(tools_provider=...)`.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from scripts.bridge_db import bootstrap
from scripts.bridge_softdrop import make_soft_drop_record
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
# Stall-anchor confirmation window. Once bridge_heartbeat.last_polled_at goes
# stale (> HEARTBEAT_STALL_S) WITHOUT monotonic corroboration, the poll loop
# anchors on the stale VALUE and resets the per-call deadline AT MOST ONCE per
# distinct value (Issue #41: each suspend/resume produces a NEW stale value,
# so legitimate resumes keep their fresh budget). If the SAME stale value then
# persists through STALL_CONFIRM_S of continuous Python ticking, S1 is
# genuinely dead — a resumed S1 refreshes its heartbeat within ~2s — and the
# loop raises BridgeStallError("... (confirmed)"). Without this window a dead
# S1 was undetectable: the loop's own per-tick Python heartbeat kept mono_gap
# at ~POLL_INTERVAL_S forever, AND the unconditional deadline reset in the
# suspend branch neutralised BridgeTimeoutError, so the poll spun forever.
STALL_CONFIRM_S: float = 300.0
POLL_INTERVAL_S: float = 0.2
STALE_ROW_S: float = 900.0
# Per-cycle outer wall-clock deadline. Bounds worst-case bridge time at
# CYCLE_WALL_S regardless of how many dispatches the cycle issues. Without
# this, a cycle that issues N requests can in principle block
# PER_CALL_TIMEOUT_S * N before any single-call timeout fires (e.g., 50 * 600s
# = 8h). 3600s is generous for legitimate multi-wave cycles but bounds the
# pathological case. Tripped via `BridgeStallError("cycle wall-clock exceeded")`.
# Issue #42 / PR review round 1 (architect MINOR finding).
#
# Operator escape hatch: set ``KAIZEN_CYCLE_WALL_S=<seconds>`` in the
# environment to override the default. Added in response to run 33
# (project-kaizen-run-33-portability-bundle) where cycle 1 cleared
# 0-BLOCKING reviewers but the 3600s cycle wall expired before
# commit/push, forcing hand-finish as PR #56. Parsing is defensive —
# malformed env vars MUST NOT abort a cycle.
_DEFAULT_CYCLE_WALL_S: float = 3600.0


def _resolve_cycle_wall_s() -> float:
    """Resolve the per-cycle wall-clock budget from ``KAIZEN_CYCLE_WALL_S``.

    Contract (Phase-3 mesh agreement, backend-engineer-1 caveat C2):

      * unset or empty string → ``_DEFAULT_CYCLE_WALL_S``
      * non-numeric           → warn to stderr, fall back to default
      * numeric and <= 0      → warn to stderr, fall back to default
      * numeric and > 0       → use it (no upper clamp — operator escape
        hatch, trust the operator)

    Resolution happens at module import time; subsequent in-process env
    mutation does not take effect (matches the existing pattern of
    overriding ``wrapper.CYCLE_WALL_S`` per-instance for tests).
    """
    raw = os.environ.get("KAIZEN_CYCLE_WALL_S")
    if raw is None or raw == "":
        return _DEFAULT_CYCLE_WALL_S
    try:
        value = float(raw)
    except ValueError:
        print(
            f"[kaizen.cc_tool_bridge] KAIZEN_CYCLE_WALL_S={raw!r} is not "
            f"numeric; falling back to default {_DEFAULT_CYCLE_WALL_S}s.",
            file=sys.stderr,
        )
        return _DEFAULT_CYCLE_WALL_S
    if value <= 0:
        print(
            f"[kaizen.cc_tool_bridge] KAIZEN_CYCLE_WALL_S={value} must be "
            f"> 0; falling back to default {_DEFAULT_CYCLE_WALL_S}s.",
            file=sys.stderr,
        )
        return _DEFAULT_CYCLE_WALL_S
    return value


CYCLE_WALL_S: float = _resolve_cycle_wall_s()


# Per-row soft-timeout for the quorum path (#83). Distinct from the batch
# hard-deadline PER_CALL_TIMEOUT_S: once a batch has met its quorum, any row
# still 'pending' that has waited longer than this soft-timeout becomes
# eligible to be soft-dropped (a synthetic absent-teammate record) so a single
# silent teammate cannot fail the whole batch. It NEVER short-circuits the hard
# backstops (PER_CALL_TIMEOUT_S / CYCLE_WALL_S / HEARTBEAT_STALL_S): those still
# govern when quorum is NOT met. Default is < PER_CALL_TIMEOUT_S so it can fire
# before the hard deadline. Operator escape hatch mirrors KAIZEN_CYCLE_WALL_S.
_DEFAULT_ROW_SOFT_TIMEOUT_S: float = 300.0


def _resolve_row_soft_timeout_s() -> float:
    """Resolve the per-row soft-timeout from ``KAIZEN_ROW_SOFT_TIMEOUT_S``.

    Same defensive-parse contract as :func:`_resolve_cycle_wall_s`:

      * unset or empty string → ``_DEFAULT_ROW_SOFT_TIMEOUT_S``
      * non-numeric           → warn to stderr, fall back to default
      * numeric and <= 0      → warn to stderr, fall back to default
      * numeric and > 0       → use it (no upper clamp)

    A malformed env var MUST NOT abort a cycle.
    """
    raw = os.environ.get("KAIZEN_ROW_SOFT_TIMEOUT_S")
    if raw is None or raw == "":
        return _DEFAULT_ROW_SOFT_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        print(
            f"[kaizen.cc_tool_bridge] KAIZEN_ROW_SOFT_TIMEOUT_S={raw!r} is not "
            f"numeric; falling back to default {_DEFAULT_ROW_SOFT_TIMEOUT_S}s.",
            file=sys.stderr,
        )
        return _DEFAULT_ROW_SOFT_TIMEOUT_S
    if value <= 0:
        print(
            f"[kaizen.cc_tool_bridge] KAIZEN_ROW_SOFT_TIMEOUT_S={value} must be "
            f"> 0; falling back to default {_DEFAULT_ROW_SOFT_TIMEOUT_S}s.",
            file=sys.stderr,
        )
        return _DEFAULT_ROW_SOFT_TIMEOUT_S
    return value


ROW_SOFT_TIMEOUT_S: float = _resolve_row_soft_timeout_s()

# Default quorum fraction for fan-out phases that opt into quorum-relaxed
# dispatch. quorum = max(1, ceil(QUORUM_FRACTION * N)). NB for small N this
# forgives nothing (N=2→2, N=3→3) — intentional: a small evidence base has no
# redundancy to spare, so soft-drop is effectively a large-wave (N>=4) feature.
QUORUM_FRACTION: float = 0.75


def quorum_for(n: int, fraction: float = QUORUM_FRACTION) -> int:
    """Return the genuine-ready row count required to satisfy quorum for a
    batch of ``n`` rows: ``max(1, ceil(fraction * n))``."""
    return max(1, math.ceil(fraction * n))


# Module-level table of per-run "last python heartbeat" monotonic timestamps.
# Keyed by run_id (multiple QueueBridgeWrapper instances in one process may
# share this table; each updates only its own row). Used by the hybrid stall
# check to distinguish a wall-clock skew (laptop suspend/resume) from a real
# S1 stall: julianday('now') can jump 8h ahead of the SQLite-stored
# bridge_heartbeat row while CLOCK_MONOTONIC (on macOS) does not advance during
# suspend; tripping BridgeStallError on the julianday signal alone would
# spuriously abandon every cycle resumed after a laptop close. The hybrid
# predicate raises only when BOTH gaps exceed HEARTBEAT_STALL_S. Issue #41.
_last_python_tick_monotonic: dict[int, float] = {}


@dataclass(frozen=True)
class BridgeTimeoutSnapshot:
    """Read-first capture of in-flight batch state at a bridge timeout/stall.

    Attached to the ``BridgeTimeoutError`` / ``BridgeStallError`` raised from
    :meth:`QueueBridgeWrapper._poll_many` so a caller (``scripts/run.py``) can
    SURFACE the teammate work that completed before a hard-backstop trip
    instead of silently discarding it (kaizen#91). Built from the poll loop's
    in-memory, cycle-accurate bookkeeping — never a ``run_id``-scoped DB query
    (``bridge_requests`` has no cycle column, so a ``run_id`` SELECT would
    sweep in prior cycles' ready rows and corrupt the N-of-M count).

    Capture + VISIBILITY only: holds decoded response payloads, counts,
    pending recipients, and a partial-vs-stall classification. It deliberately
    carries NO db handle, branch name, or resume token — recovery stays manual.
    """

    completed_count: int
    total: int
    pending_count: int
    soft_dropped_count: int
    classification: str  # "partial_progress" | "true_stall"
    completed_responses: tuple[dict, ...]
    pending_recipients: tuple[str, ...]


class BridgeError(RuntimeError):
    """Base class for queue-bridge failures observable by Python.

    kaizen#91: every ``Bridge*Error`` optionally carries a
    :class:`BridgeTimeoutSnapshot` (``None`` unless raised from a batch
    timeout/stall in ``_poll_many``) so the in-flight work that completed
    before the trip is recoverable rather than silently discarded. The
    keyword-only ``snapshot`` param is fully back-compatible — every existing
    ``BridgeX("message")`` call site keeps working and gets ``snapshot=None``.
    """

    def __init__(self, *args: object, snapshot: BridgeTimeoutSnapshot | None = None) -> None:
        super().__init__(*args)
        self.snapshot = snapshot


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
    STALL_CONFIRM_S: float = STALL_CONFIRM_S
    POLL_INTERVAL_S: float = POLL_INTERVAL_S
    STALE_ROW_S: float = STALE_ROW_S
    CYCLE_WALL_S: float = CYCLE_WALL_S
    ROW_SOFT_TIMEOUT_S: float = ROW_SOFT_TIMEOUT_S

    def __init__(self, db_path: str | Path, run_id: int):
        self._db_path = Path(db_path)
        self._run_id = int(run_id)
        # Per-cycle wall-clock deadline; set lazily on the first _request()
        # call so the deadline measures from first dispatch, not wrapper
        # construction. reset_cycle_deadline() puts it back to None when a
        # cycle finalizes and the same wrapper instance is to be reused.
        # New wrapper instances also start with None — so the production
        # path (a fresh wrapper per tools_provider() call per cycle) gets
        # automatic reset by construction. Issue #42.
        self._cycle_deadline: float | None = None
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

    def send_message_many(
        self, messages: list[dict], *, quorum_floor: int | None = None
    ) -> list[str]:
        """Batch variant of send_message — enqueue N rows in one transaction
        and wait for the batch to resolve before returning the responses in
        INPUT ORDER.

        Each dict in ``messages`` must have keys ``team_id`` (str), ``to``
        (str), ``message`` (str). Returns a list[str] of
        ``response_json["response"]`` strings, in the same order as input
        ``messages``.

        ``quorum_floor`` (#83): when ``None`` (default) the batch is STRICT —
        every row must reach ``status='ready'`` (the original contract). When
        an int, the batch is quorum-relaxed (see :meth:`_poll_many`): once
        ``quorum_floor`` rows are genuinely ready and the per-row soft-timeout
        has elapsed, silent stragglers are soft-dropped and their slot carries
        the ``SOFT_DROP_SENTINEL``-prefixed response string. Callers that
        dispatch a SAFETY-CRITICAL phase (e.g. an independent reviewer or a
        state-mutating gate) MUST leave this ``None`` so a silent reviewer can
        never be forgiven by a contributor quorum.

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
        responses = self._poll_many(
            row_ids, kind="send_message", messages=messages, quorum_floor=quorum_floor
        )
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
        #
        # #83: cleanup=True bypasses the per-cycle wall-clock (teardown often
        # runs *because* the wall expired). And teardown is BEST-EFFORT — a
        # teardown that raises is not a teardown: we swallow Bridge*Error and
        # let the caller fall through to the L1-L4 filesystem/pkill cleanup
        # rather than abort the run on a failed reap. Three bounds keep this
        # from blocking forever: the per-call CLEANUP_TIMEOUT_S deadline, the
        # stall guard (both-clocks raise + STALL_CONFIRM_S confirmed raise),
        # and — because a stale-but-unconfirmed heartbeat legitimately resets
        # the per-call deadline — the never-reset cleanup hard cap
        # (2*CLEANUP_TIMEOUT_S + STALL_CONFIRM_S) in `_request`.
        try:
            self._request(
                "team_delete",
                {"team_id": team_id},
                timeout_s=self.CLEANUP_TIMEOUT_S,
                cleanup=True,
            )
        except BridgeError as exc:
            print(
                f"[kaizen.cc_tool_bridge] team_delete({team_id!r}) did not "
                f"complete via the bridge ({type(exc).__name__}: {exc}); "
                f"continuing — filesystem/pkill teardown is the backstop.",
                file=sys.stderr,
            )

    def apply_layout(self, team_id: str) -> None:
        # kaizen#86: the workspace fold MUST run in the orchestrator session
        # (whose $TMUX/$TMUX_PANE point at the window holding the teammate
        # panes), NOT in this detached run_bridged process — whose tmux commands
        # never reach that window, so the in-process fold is a silent no-op and
        # the panes stay a single stacked column. So we enqueue an `apply_layout`
        # bridge request; the orchestrator services it by running
        # `python3 -m scripts.fold_workspace` (see skills/improve/SKILL.md).
        #
        # Best-effort + cosmetic: a layout that fails to apply MUST NOT abort the
        # cycle, so we swallow Bridge*Error (mirrors team_delete). The cleanup
        # bypass is reused so a layout request late in a wall-clock-pressed cycle
        # still gets a chance rather than tripping the cycle-wall.
        try:
            self._request(
                "apply_layout",
                {"team_id": team_id},
                timeout_s=self.CLEANUP_TIMEOUT_S,
                cleanup=True,
            )
        except BridgeError as exc:
            print(
                f"[kaizen.cc_tool_bridge] apply_layout({team_id!r}) did not "
                f"complete via the bridge ({type(exc).__name__}: {exc}); "
                f"continuing — layout is cosmetic, the cycle proceeds.",
                file=sys.stderr,
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

    @staticmethod
    def _build_timeout_snapshot(
        row_ids: list[int],
        id_to_index: dict[int, int],
        id_to_recipient: dict[int, str],
        completed: set[int],
        soft_dropped: set[int],
        results: list[dict | None],
    ) -> BridgeTimeoutSnapshot:
        """Capture in-flight batch state at a hard-backstop trip (kaizen#91).

        Built from :meth:`_poll_many`'s batch-local bookkeeping so the work
        that ALREADY completed is recoverable rather than silently discarded.
        Cycle-accurate by construction: reads ONLY this batch's in-memory
        decoded responses. ``completed_responses`` preserves INPUT order; a
        soft-dropped row counts as neither completed nor pending (it is
        forgiven-silence under quorum, not survived work and not a failure).
        """
        completed_responses = tuple(
            (results[id_to_index[rid]] or {}) for rid in row_ids if rid in completed
        )
        pending_ids = [rid for rid in row_ids if rid not in completed and rid not in soft_dropped]
        pending_recipients = tuple(id_to_recipient[rid] for rid in pending_ids)
        return BridgeTimeoutSnapshot(
            completed_count=len(completed),
            total=len(row_ids),
            pending_count=len(pending_ids),
            soft_dropped_count=len(soft_dropped),
            classification="partial_progress" if completed else "true_stall",
            completed_responses=completed_responses,
            pending_recipients=pending_recipients,
        )

    def _poll_many(
        self,
        row_ids: list[int],
        *,
        kind: str,
        messages: list[dict],
        quorum_floor: int | None = None,
    ) -> list[dict]:
        """Poll N bridge_requests rows in lock-step. Returns the decoded
        ``response_json`` dicts in INPUT ORDER (aligned to ``row_ids``).

        Per-batch deadline budget = ``PER_CALL_TIMEOUT_S`` (applied to the
        SLOWEST row — same semantics as ``_request`` but scoped to the
        whole batch). Per-batch stall budget = ``HEARTBEAT_STALL_S``.

        Mirrors the single-row poll loop (``_request``) — same stall and
        timeout invariants, just SELECT'ing N rows at once.

        Quorum (#83). When ``quorum_floor`` is ``None`` (the default) the
        batch is STRICT: every row must reach ``status='ready'`` (the
        original all-or-nothing contract). When ``quorum_floor`` is an int,
        the batch is quorum-relaxed: once at least ``quorum_floor`` rows are
        GENUINELY ``ready`` AND every still-``pending`` row has waited longer
        than ``ROW_SOFT_TIMEOUT_S``, the remaining pending rows are
        SOFT-DROPPED — their result slot is filled (in input order) with a
        synthetic absent-teammate record from
        :func:`scripts.bridge_softdrop.make_soft_drop_record` and the batch
        returns. Invariants:

          * Quorum forgives SILENCE only, never FAILURE: ``status='error'``
            and disappeared rows still raise ``BridgeRemoteError`` — those
            checks run BEFORE the quorum-satisfied return each tick, so a
            failure can never be masked by a met quorum.
          * Only GENUINELY ready rows count toward ``quorum_floor`` — a
            soft-drop can never satisfy quorum.
          * No drop before quorum: until ``quorum_floor`` genuine-ready rows
            exist, the hard backstops (PER_CALL_TIMEOUT_S / CYCLE_WALL_S /
            heartbeat-stall) are the SOLE governors, unchanged.
          * Grace after quorum: a still-pending row that has NOT yet exceeded
            ``ROW_SOFT_TIMEOUT_S`` keeps being awaited even when quorum is
            already met — we never drop a teammate who may be about to answer.
          * Soft-dropped rows are left ``status='pending'`` in the DB (the
            orchestrator S1 owns row lifecycle); the synthetic record lives
            only in the returned list. A late genuine reply that flips the row
            to ``ready`` after we return is harmless — we no longer read it.
        """
        # Map row_id → (input_index, recipient) for error attribution
        id_to_index = {rid: i for i, rid in enumerate(row_ids)}
        id_to_recipient = {rid: messages[i].get("to", "?") for i, rid in enumerate(row_ids)}
        # Placeholder list ordered by input index.
        results: list[dict | None] = [None] * len(row_ids)
        completed: set[int] = set()
        placeholders = ",".join("?" for _ in row_ids)
        deadline = time.monotonic() + self.PER_CALL_TIMEOUT_S
        # Lazy-init per-cycle outer deadline (Issue #42). Mirrors _request.
        if self._cycle_deadline is None:
            self._cycle_deadline = time.monotonic() + self.CYCLE_WALL_S
        # Quorum bookkeeping (#83). batch_start anchors the per-row
        # soft-timeout: _insert_many enqueues every row in ONE transaction so
        # each row's first-seen time == batch_start. `soft_dropped` tracks rows
        # filled with a synthetic absent record; those NEVER count toward
        # quorum. `effective_quorum` is None for the strict (all-N) path.
        batch_start = time.monotonic()
        soft_dropped: set[int] = set()
        effective_quorum = (
            None if quorum_floor is None else max(1, min(int(quorum_floor), len(row_ids)))
        )
        # Stall anchor — mirrors `_request`; see the STALL_CONFIRM_S module
        # comment and the three-way stall decision documented there.
        stall_anchor: tuple[str, float] | None = None
        while True:
            # Capture monotonic gap BEFORE this iteration's tick — see the
            # Issue #41 rationale in `_request` for the why.
            mono_gap = self._python_monotonic_gap()
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
                if rid not in seen_ids and rid not in completed and rid not in soft_dropped:
                    idx = id_to_index[rid]
                    recipient = id_to_recipient[rid]
                    raise BridgeRemoteError(
                        f"send_message_many: messages[{idx}] (to={recipient!r}) "
                        f"row {rid} disappeared from queue"
                    )
            for rid, status, response_json, error_text in rows:
                if rid in completed or rid in soft_dropped:
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

            # Return-decision (#83). error/disappeared raises above already
            # ran this tick, so a met quorum can never mask a failure.
            if effective_quorum is None:
                # STRICT path: every row must be genuinely ready.
                if len(completed) == len(row_ids):
                    return [r if r is not None else {} for r in results]
            else:
                # QUORUM path: only GENUINELY ready rows count toward quorum.
                if len(completed) >= effective_quorum and (
                    time.monotonic() - batch_start >= self.ROW_SOFT_TIMEOUT_S
                ):
                    # Quorum met AND past the soft-timeout: soft-drop every
                    # still-pending straggler (grace window has elapsed). A row
                    # still inside its soft-timeout is left pending and keeps
                    # being awaited on the next tick.
                    for rid in row_ids:
                        if rid not in completed and rid not in soft_dropped:
                            idx = id_to_index[rid]
                            recipient = id_to_recipient[rid]
                            results[idx] = make_soft_drop_record(
                                idx,
                                recipient,
                                "row never reached ready before soft-timeout",
                            )
                            soft_dropped.add(rid)
                if len(completed) + len(soft_dropped) == len(row_ids):
                    # Either all rows are genuinely ready, or quorum was met and
                    # the stragglers were soft-dropped — batch is resolved.
                    return [r if r is not None else {} for r in results]

            # Stall + deadline checks — same shape as _request, just batch-scoped.
            # Three-way stall decision (Issue #41 + stall-anchor fix) —
            # see the block comment in `_request` for the full rationale:
            # both-clocks agree → raise now; stale without monotonic
            # corroboration → anchor on the stale value, resetting the
            # per-batch deadline at most ONCE per distinct value, and
            # raise (confirmed) if the SAME value persists through
            # STALL_CONFIRM_S of continuous ticking; fresh → clear anchor.
            hb = self._s1_heartbeat_raw()
            stall = None if hb is None else hb[0]
            if stall is not None and stall > self.HEARTBEAT_STALL_S:
                if mono_gap is not None and mono_gap > self.HEARTBEAT_STALL_S:
                    pending_ids = [rid for rid in row_ids if rid not in completed]
                    raise BridgeStallError(
                        f"S1 heartbeat stalled during send_message_many batch: "
                        f"last_polled_at is {stall:.1f}s old "
                        f"(> HEARTBEAT_STALL_S={self.HEARTBEAT_STALL_S}s); "
                        f"python monotonic gap is "
                        f"{mono_gap if mono_gap is None else f'{mono_gap:.1f}s'}; "
                        f"{len(pending_ids)} of {len(row_ids)} rows still pending "
                        f"(rows={pending_ids})",
                        snapshot=self._build_timeout_snapshot(
                            row_ids, id_to_index, id_to_recipient, completed, soft_dropped, results
                        ),
                    )
                if stall_anchor is None or stall_anchor[0] != hb[1]:
                    # NEW stale value (suspend/resume or first observation):
                    # re-anchor; deadline resets at most once per value.
                    stall_anchor = (hb[1], time.monotonic())
                    deadline = time.monotonic() + self.PER_CALL_TIMEOUT_S
                elif time.monotonic() - stall_anchor[1] > self.STALL_CONFIRM_S:
                    pending_ids = [rid for rid in row_ids if rid not in completed]
                    raise BridgeStallError(
                        f"S1 heartbeat stalled (confirmed) during "
                        f"send_message_many batch: last_polled_at ({hb[1]}) "
                        f"unchanged for "
                        f"{time.monotonic() - stall_anchor[1]:.1f}s of "
                        f"continuous Python ticking "
                        f"(> STALL_CONFIRM_S={self.STALL_CONFIRM_S}s) and is "
                        f"{stall:.1f}s old "
                        f"(> HEARTBEAT_STALL_S={self.HEARTBEAT_STALL_S}s); "
                        f"{len(pending_ids)} of {len(row_ids)} rows still pending "
                        f"(rows={pending_ids})",
                        snapshot=self._build_timeout_snapshot(
                            row_ids, id_to_index, id_to_recipient, completed, soft_dropped, results
                        ),
                    )
            else:
                stall_anchor = None

            # Per-cycle outer wall-clock bound (Issue #42).
            if time.monotonic() >= self._cycle_deadline:
                pending_ids = [rid for rid in row_ids if rid not in completed]
                elapsed = time.monotonic() - (self._cycle_deadline - self.CYCLE_WALL_S)
                raise BridgeStallError(
                    f"cycle wall-clock exceeded during send_message_many batch: "
                    f"{elapsed:.1f}s elapsed (> CYCLE_WALL_S={self.CYCLE_WALL_S}s); "
                    f"{len(pending_ids)} of {len(row_ids)} rows still pending "
                    f"(rows={pending_ids})",
                    snapshot=self._build_timeout_snapshot(
                        row_ids, id_to_index, id_to_recipient, completed, soft_dropped, results
                    ),
                )

            if time.monotonic() >= deadline:
                pending_ids = [rid for rid in row_ids if rid not in completed]
                raise BridgeTimeoutError(
                    f"send_message_many: {len(pending_ids)} of {len(row_ids)} rows "
                    f"timed out after {self.PER_CALL_TIMEOUT_S}s "
                    f"(S1 heartbeat alive, but rows never reached 'ready'); "
                    f"pending rows={pending_ids}",
                    snapshot=self._build_timeout_snapshot(
                        row_ids, id_to_index, id_to_recipient, completed, soft_dropped, results
                    ),
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
        waiting on takes a long time to come back.

        Also records the current monotonic timestamp into the module-level
        ``_last_python_tick_monotonic`` table keyed by ``run_id`` — the
        hybrid stall predicate reads this to distinguish wall-clock skew
        from real S1 silence (Issue #41).
        """
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
        _last_python_tick_monotonic[self._run_id] = time.monotonic()

    def _s1_heartbeat_raw(self) -> tuple[float, str] | None:
        """Return ``(age_seconds, last_polled_at_raw)`` for S1's
        bridge_heartbeat row, or None if S1 has not yet ticked once.
        Uses julianday() for the age computation (MINOR-PYTHON-HB-CHECK).

        m7 (review round 1): the None return is asymmetric on purpose
        — the poll loops treat None as "S1 still booting, assume
        alive" and fall through to the per-call timeout check. A
        present-and-stale heartbeat (age > HEARTBEAT_STALL_S) is
        necessary-but-not-sufficient to trip BridgeStallError; see
        ``_python_monotonic_gap`` and the stall-anchor logic in
        ``_request`` for the corroborating conditions. Rationale:
        on a cold start S1 fires its first bridge_heartbeat UPSERT
        inside the FIRST iteration of the poll loop — there is a
        small window before that first tick where the row simply
        doesn't exist; raising BridgeStallError then would abandon
        every cycle on its first request.

        The RAW ``last_polled_at`` string is the stall-anchor identity:
        the poll loops compare successive raw values to distinguish a
        dead S1 (value never changes) from repeated suspend/resume
        cycles (each suspend leaves a NEW stale value). Issue #41.
        """
        con = _connect(self._db_path)
        try:
            cur = con.execute(
                "SELECT (julianday('now') - julianday(last_polled_at)) * 86400, "
                "last_polled_at "
                "FROM bridge_heartbeat WHERE run_id = ?",
                (self._run_id,),
            )
            row = cur.fetchone()
            # No row OR NULL value → "S1 hasn't ticked yet": assume
            # alive (still booting).
            if row is None or row[0] is None:
                return None
            return (float(row[0]), str(row[1]))
        finally:
            con.close()

    def _s1_seconds_since_last_poll(self) -> float | None:
        """Return wall-clock seconds since S1's last bridge_heartbeat
        tick (or None if S1 has not yet ticked once). Thin delegate to
        :meth:`_s1_heartbeat_raw` — kept for callers/tests that only
        need the age."""
        hb = self._s1_heartbeat_raw()
        return None if hb is None else hb[0]

    def _python_monotonic_gap(self) -> float | None:
        """Return monotonic seconds since this wrapper last UPSERTed
        ``python_heartbeat`` (i.e., since the last ``_tick_python_heartbeat``
        for our run_id), or None if we have not ticked yet.

        The hybrid stall predicate uses this in conjunction with
        ``_s1_seconds_since_last_poll``: a large julianday gap with a small
        monotonic gap means the wall-clock skew is local to SQLite's clock
        (laptop suspend/resume on platforms where CLOCK_MONOTONIC pauses
        across suspend) — NOT a real S1 stall. See Issue #41.
        """
        last = _last_python_tick_monotonic.get(self._run_id)
        if last is None:
            return None
        return time.monotonic() - last

    def reset_cycle_deadline(self) -> None:
        """Clear the per-cycle outer deadline so the next ``_request()`` call
        starts a fresh ``CYCLE_WALL_S`` budget. The production path constructs
        a new wrapper per cycle via ``queue_bridge_provider`` (auto-reset by
        construction), so this method is the explicit escape hatch for
        callers that retain a wrapper instance across cycles or for
        defensive use in cycle finalization (Issue #42)."""
        self._cycle_deadline = None

    def _request(
        self,
        kind: str,
        args: dict,
        *,
        timeout_s: float | None = None,
        cleanup: bool = False,
    ) -> dict:
        """Enqueue + poll one bridge_requests row. Returns the decoded
        `response_json` dict (or raises one of the Bridge*Error).

        Cycle wall-clock bound (Issue #42): on the first ``_request()`` call
        per cycle the wrapper sets ``self._cycle_deadline`` to
        ``time.monotonic() + CYCLE_WALL_S``. Subsequent calls inherit the
        same deadline; once exceeded any call raises
        ``BridgeStallError("cycle wall-clock exceeded")``. The deadline
        resets when a new wrapper is constructed (the production path) or
        when ``reset_cycle_deadline()`` is called explicitly.

        ``cleanup`` (#83): teardown-path requests (``team_delete``) set this
        to ``True``. It BYPASSES the per-cycle wall-clock — and does not
        initialise it — because teardown most often runs precisely BECAUSE the
        cycle wall already expired; without the bypass, ``team_delete`` would
        raise ``BridgeStallError("cycle wall-clock exceeded")`` on the first
        poll before cleanup could complete, leaking the very teammate
        processes/panes the teardown exists to reap. The heartbeat-stall guard
        stays active (you cannot tear down through a dead bridge: either the
        both-clocks raise or the ``STALL_CONFIRM_S`` confirmed-stall raise
        fires) and a NEVER-RESET hard cap of ``2 * timeout_s +
        STALL_CONFIRM_S`` backstops the cleanup path — the per-call
        ``timeout_s`` deadline alone is insufficient because a stale-but-
        unconfirmed heartbeat legitimately resets it (suspend/resume), and
        without the cap a cleanup call (which skips the cycle wall) had no
        remaining bound at all.
        """
        timeout_s = self.PER_CALL_TIMEOUT_S if timeout_s is None else timeout_s
        # Lazy-initialize the per-cycle outer deadline on first dispatch.
        # Skip for cleanup-path calls: teardown must not start (or be bounded
        # by) the cycle clock.
        if not cleanup and self._cycle_deadline is None:
            self._cycle_deadline = time.monotonic() + self.CYCLE_WALL_S
        row_id = self._insert(kind, args)
        deadline = time.monotonic() + timeout_s
        # Cleanup-path hard cap: cleanup calls skip the per-cycle wall, and
        # the per-call deadline can be legitimately reset once per distinct
        # stale heartbeat value — so a NEVER-reassigned monotonic cap is the
        # last-line bound that keeps team_delete/apply_layout from spinning
        # forever against a stale S1. BridgeTimeoutError is a BridgeError, so
        # those callers swallow it and fall to the L1-L4 backstop.
        hard_cap = time.monotonic() + 2 * timeout_s + self.STALL_CONFIRM_S if cleanup else None
        # Stall anchor (see STALL_CONFIRM_S module comment): (raw
        # last_polled_at value, monotonic time first observed stale).
        stall_anchor: tuple[str, float] | None = None
        while True:
            # Hybrid stall predicate (Issue #41 — laptop-suspend skew):
            # capture the monotonic gap BEFORE this iteration's tick so it
            # reflects elapsed time since the PREVIOUS tick (i.e., one
            # iteration ago). Normal cadence is ~POLL_INTERVAL_S; a value
            # exceeding HEARTBEAT_STALL_S means Python was unable to tick
            # for that long — which is the corroborating signal we need
            # alongside the SQLite-side julianday gap to distinguish a
            # genuine S1 stall (Python kept running, S1 stopped) from a
            # suspend/resume artefact (BOTH paused, neither is "gone").
            mono_gap = self._python_monotonic_gap()
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
            # Stall decision, three-way (Issue #41 + stall-anchor fix):
            #
            #   1. BOTH the SQLite wall-clock gap AND the monotonic gap
            #      (since the prior iteration's tick) exceed
            #      HEARTBEAT_STALL_S → real stall, raise immediately.
            #      (Reachable only when Python itself was unable to tick
            #      for that long — e.g. a pre-seeded/stalled process.)
            #   2. Stale julianday gap WITHOUT monotonic corroboration —
            #      either suspend/resume skew or a dead S1; the two are
            #      indistinguishable on a single reading because this
            #      loop's own per-tick Python heartbeat keeps mono_gap at
            #      ~POLL_INTERVAL_S. Disambiguate via the stall ANCHOR:
            #      on each NEW stale last_polled_at value, re-anchor and
            #      reset the per-call deadline AT MOST ONCE (each suspend
            #      produces a fresh stale value, so resumes keep their
            #      budget); if the SAME stale value persists through
            #      STALL_CONFIRM_S of continuous ticking, S1 is dead —
            #      a resumed S1 refreshes its heartbeat within seconds.
            #   3. Heartbeat fresh (or absent — S1 still booting) → clear
            #      the anchor and let the deadline checks govern.
            #
            # `mono_gap is None` (no prior tick for this run_id — e.g. a
            # fresh wrapper instance constructed for a new cycle after
            # the laptop resumed from suspend) means NO monotonic
            # evidence; conservatively treat as case 2 (Issue #41 + SDET
            # review on PR for #40-#45) — the anchor still bounds it.
            hb = self._s1_heartbeat_raw()
            stall = None if hb is None else hb[0]
            if stall is not None and stall > self.HEARTBEAT_STALL_S:
                if mono_gap is not None and mono_gap > self.HEARTBEAT_STALL_S:
                    raise BridgeStallError(
                        f"S1 heartbeat stalled: last_polled_at is "
                        f"{stall:.1f}s old "
                        f"(> HEARTBEAT_STALL_S={self.HEARTBEAT_STALL_S}s); "
                        f"python monotonic gap is "
                        f"{mono_gap if mono_gap is None else f'{mono_gap:.1f}s'}; "
                        f"row {row_id} ({kind}) abandoned"
                    )
                if stall_anchor is None or stall_anchor[0] != hb[1]:
                    # NEW stale value (first observation, or a fresh
                    # suspend/resume left a different last_polled_at):
                    # re-anchor and reset the per-call deadline so the
                    # suspend window does not eat into the legitimate
                    # per-call budget — at most once per distinct value.
                    stall_anchor = (hb[1], time.monotonic())
                    deadline = time.monotonic() + timeout_s
                elif time.monotonic() - stall_anchor[1] > self.STALL_CONFIRM_S:
                    raise BridgeStallError(
                        f"S1 heartbeat stalled (confirmed): last_polled_at "
                        f"({hb[1]}) unchanged for "
                        f"{time.monotonic() - stall_anchor[1]:.1f}s of "
                        f"continuous Python ticking "
                        f"(> STALL_CONFIRM_S={self.STALL_CONFIRM_S}s) and is "
                        f"{stall:.1f}s old "
                        f"(> HEARTBEAT_STALL_S={self.HEARTBEAT_STALL_S}s); "
                        f"row {row_id} ({kind}) abandoned"
                    )
            else:
                stall_anchor = None

            # Cleanup-path hard cap — checked unconditionally, NEVER
            # reassigned (see init above the loop).
            if hard_cap is not None and time.monotonic() >= hard_cap:
                raise BridgeTimeoutError(
                    f"row {row_id} ({kind}) cleanup hard cap exceeded "
                    f"(2*{timeout_s}s + STALL_CONFIRM_S="
                    f"{self.STALL_CONFIRM_S}s); abandoning best-effort "
                    f"cleanup — filesystem/pkill backstop takes over"
                )

            # Per-cycle outer wall-clock bound. Applies across all calls in
            # the cycle; if the aggregate budget is blown, abandon. Skipped on
            # the cleanup path (#83) — teardown must complete even when the
            # cycle wall has already expired.
            if (
                not cleanup
                and self._cycle_deadline is not None
                and time.monotonic() >= self._cycle_deadline
            ):
                elapsed = time.monotonic() - (self._cycle_deadline - self.CYCLE_WALL_S)
                raise BridgeStallError(
                    f"cycle wall-clock exceeded: {elapsed:.1f}s elapsed "
                    f"(> CYCLE_WALL_S={self.CYCLE_WALL_S}s); "
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
