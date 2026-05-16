"""Per-cycle DB infrastructure.

`cycle.py` does NOT run the multi-agent meeting itself — that lives in
`internal/cycle/SKILL.md` prose (Wave 7). This module provides the
infrastructure that the SKILL prose (or a test fake) calls into:

  - record_cycle_success / record_cycle_abandoned: DB row inserts
  - get_cycle / list_cycles: read helpers
  - execute_cycle: a stub executor (NotImplementedError) that the
    orchestrator (`run.py`) calls when no explicit cycle_executor is
    injected. Tests inject a fake; Wave 7 will wire the real meeting.

# DESIGN NOTE
The `cycles.status` column is CHECK-constrained to ('success', 'abandoned').
There is no 'running' state in the schema. We chose option (a) from the plan:
insert the row only AFTER the cycle terminates. This keeps the schema simple
and avoids a sentinel state. The trade-off: a mid-cycle crash leaves no
`cycles` row. Post-mortem analysis can still detect unaccounted cycles by
comparing `runs.cycles_requested` against `cycles_succeeded + cycles_abandoned`.
"""
from __future__ import annotations

from datetime import datetime, timezone

from scripts.db import get_connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row, cols) -> dict:
    return dict(zip(cols, row))


# ── DB writes ───────────────────────────────────────────────────────────────

def record_cycle_success(
    db_path: str,
    run_id: int,
    cycle_n: int,
    subject: str | None,
    commit_sha: str,
    minutes_memex_slug: str | None,
    started_at: str,
    ended_at: str | None = None,
) -> dict:
    """Insert a cycles row with status='success'. Returns the inserted row."""
    if ended_at is None:
        ended_at = _now()
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO cycles "
            "(run_id, cycle_n, subject, status, commit_sha, minutes_memex_slug, "
            " started_at, ended_at) "
            "VALUES (?, ?, ?, 'success', ?, ?, ?, ?)",
            (run_id, cycle_n, subject, commit_sha, minutes_memex_slug,
             started_at, ended_at),
        )
        conn.commit()
        new_id = cur.lastrowid
    finally:
        conn.close()
    return get_cycle(db_path, new_id)


def record_cycle_abandoned(
    db_path: str,
    run_id: int,
    cycle_n: int,
    subject: str | None,
    started_at: str,
    ended_at: str | None = None,
) -> dict:
    """Insert a cycles row with status='abandoned'. Returns the inserted row.

    The caller typically takes the returned `id` and passes it to
    `scripts.abandonment.record_abandonment` to write the matching
    abandonments row.
    """
    if ended_at is None:
        ended_at = _now()
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO cycles "
            "(run_id, cycle_n, subject, status, commit_sha, minutes_memex_slug, "
            " started_at, ended_at) "
            "VALUES (?, ?, ?, 'abandoned', NULL, NULL, ?, ?)",
            (run_id, cycle_n, subject, started_at, ended_at),
        )
        conn.commit()
        new_id = cur.lastrowid
    finally:
        conn.close()
    return get_cycle(db_path, new_id)


def get_cycle(db_path: str, cycle_id: int) -> dict | None:
    conn = get_connection(db_path)
    try:
        cur = conn.execute("SELECT * FROM cycles WHERE id = ?", (cycle_id,))
        row = cur.fetchone()
        if row is None:
            return None
        cols = [c[0] for c in cur.description]
        return _row_to_dict(row, cols)
    finally:
        conn.close()


def list_cycles(db_path: str, run_id: int) -> list[dict]:
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "SELECT * FROM cycles WHERE run_id = ? ORDER BY cycle_n",
            (run_id,),
        )
        rows = cur.fetchall()
        cols = [c[0] for c in cur.description]
        return [_row_to_dict(r, cols) for r in rows]
    finally:
        conn.close()


# ── Stub executor (Wave 7 fills this in via SKILL prose) ────────────────────

def execute_cycle(clone_dir, project: dict, run_row: dict, cycle_n: int) -> dict:
    """Default cycle executor — Wave 4 stub.

    The real multi-agent cycle is executed by the agent following
    `internal/cycle/SKILL.md` prose (Wave 7), not by Python. This stub
    raises NotImplementedError so the orchestrator fails loudly if invoked
    without a `cycle_executor` injection.
    """
    raise NotImplementedError(
        "Cycle execution prose lives in internal/cycle/SKILL.md (Wave 7). "
        "Use the cycle_executor parameter to inject a test or future "
        "implementation."
    )
