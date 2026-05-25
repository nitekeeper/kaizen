"""Ad-hoc bootstrap for `.ai/bridge.db`.

The bridge DB is per-machine and ephemeral relative to a single
`/kaizen:improve --mode team` invocation. It does NOT participate in
`scripts/migrate.py` — the migrations directory is reserved for
`.ai/memex.db` (kaizen's primary state DB). This module owns the bridge
DB's lifecycle: create-on-demand, idempotent re-bootstrap, and
bootstrap-time row purge.

Per the python-cc-tool-bridge design (Rev 4, Decision D2):

  - Three tables: bridge_requests, bridge_heartbeat, python_heartbeat.
  - CREATE TABLE IF NOT EXISTS for every statement so bootstrap() is
    safe to call repeatedly.
  - PRAGMA journal_mode=WAL and PRAGMA busy_timeout=5000 set at every
    connection open (PRAGMAs are connection-scoped in SQLite).

Cross-run row retention (issue #40):

  Every `bootstrap()` call also runs `purge_old_rows()` with a 7-day
  default retention. The bridge DB is described as "ephemeral per run"
  but its three tables accumulate ~10x per cycle post-GAP-4 -- without
  purging, stale rows pile up indefinitely across runs. 7 days is
  comfortably longer than any plausible kaizen run (matches the
  `sweep_leaked_teams` orphan window) and short enough to keep the DB
  small. Override via the `purge_age_s` kwarg if you need different
  retention.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# Embedded schema — verbatim from the design doc's "Schema (Rev 4
# consolidated)" section. CREATE TABLE IF NOT EXISTS makes bootstrap()
# idempotent: re-running on a populated DB is a no-op.
_BRIDGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS bridge_requests (
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
CREATE INDEX IF NOT EXISTS idx_bridge_requests_run_pending
    ON bridge_requests(run_id, status, id);
CREATE TABLE IF NOT EXISTS bridge_heartbeat (
    run_id         INTEGER PRIMARY KEY,
    last_polled_at TEXT NOT NULL,
    polled_count   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS python_heartbeat (
    run_id       INTEGER PRIMARY KEY,
    last_beat_at TEXT NOT NULL,
    beat_count   INTEGER NOT NULL DEFAULT 0
);
"""

# Default retention: 7 days. Matches the `sweep_leaked_teams` orphan
# window and is comfortably longer than any plausible kaizen run.
_DEFAULT_PURGE_AGE_S = 7 * 86400

# Per-table purge config: (table, timestamp_column, extra_where_clause).
# All three timestamp columns are TEXT datetime strings (ISO-8601 via
# `datetime('now')`) so we use `julianday('now') - julianday(<col>)` for
# the comparison — robust to textual representation and timezone-stable
# when both sides go through julianday().
#
# `extra_where_clause` is either None (no extra predicate) or a raw SQL
# fragment AND'd into the DELETE's WHERE. For `bridge_requests` we exclude
# rows still in `pending` status — a row that is still pending after 7
# days is almost certainly stuck, but deleting it would yank an in-flight
# row out from under the poller (which would then see "row disappeared").
# Heartbeat tables have no status column; their `extra_where_clause` is
# None.
_PURGE_TARGETS: tuple[tuple[str, str, str | None], ...] = (
    ("bridge_requests", "created_at", "status != 'pending'"),
    ("bridge_heartbeat", "last_polled_at", None),
    ("python_heartbeat", "last_beat_at", None),
)


def _connect(bridge_db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with the bridge's standard PRAGMAs applied.

    Both `journal_mode=WAL` and `busy_timeout=5000` (MINOR-ATTACH-WAL
    remediation) are connection-scoped in SQLite, so every consumer
    of the bridge DB MUST go through this helper or re-issue both
    pragmas itself.
    """
    con = sqlite3.connect(str(bridge_db_path))
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA busy_timeout = 5000;")
    return con


def purge_old_rows(
    con: sqlite3.Connection,
    cutoff_age_s: int = _DEFAULT_PURGE_AGE_S,
) -> dict[str, int]:
    """DELETE rows older than `cutoff_age_s` from all three bridge tables.

    Operates on an existing open connection so callers can compose this
    with their own transaction/PRAGMA context (e.g. `bootstrap()`
    chains it onto the same connection that just applied the schema).

    Args:
        con: open sqlite3 connection to the bridge DB.
        cutoff_age_s: rows whose timestamp is older than this many
            seconds (relative to `julianday('now')`) are deleted.
            Default 7 days.

    Returns:
        Mapping of table name → number of rows deleted. Tables that
        didn't exist yet (extremely unlikely after bootstrap, but
        guarded for robustness) map to 0.
    """
    # Convert seconds to fractional days for julianday() arithmetic.
    cutoff_days = cutoff_age_s / 86400.0
    deleted: dict[str, int] = {}
    for table, ts_col, extra_where in _PURGE_TARGETS:
        extra = f" AND ({extra_where})" if extra_where else ""
        # nosec B608 -- table, ts_col, and extra_where are all hardcoded in
        # the _PURGE_TARGETS tuple above; the only user-controlled value
        # (cutoff_days) is bound via the ? parameter.
        cur = con.execute(
            f"DELETE FROM {table} "  # nosec B608
            f"WHERE (julianday('now') - julianday({ts_col})) > ?{extra}",
            (cutoff_days,),
        )
        deleted[table] = int(cur.rowcount or 0)
    con.commit()
    return deleted


def bootstrap(
    bridge_db_path: str | Path = ".ai/bridge.db",
    purge_age_s: int = _DEFAULT_PURGE_AGE_S,
) -> None:
    """Create the bridge DB (and parent dir) if absent; idempotent.

    Safe to call repeatedly — every statement in `_BRIDGE_SCHEMA` uses
    CREATE TABLE IF NOT EXISTS, so a partially-bootstrapped DB
    self-heals. Sets PRAGMA journal_mode=WAL and PRAGMA
    busy_timeout=5000 on the connection used to apply the schema.

    After the schema is applied, runs `purge_old_rows()` with
    `purge_age_s` retention to keep the DB from growing unboundedly
    across runs (issue #40). Set `purge_age_s` very large to effectively
    disable the sweep — but note that callers wanting full control
    should call `purge_old_rows()` directly on their own connection.

    Args:
        bridge_db_path: filesystem path to the bridge DB. Default
            `.ai/bridge.db` (relative to the kaizen repo root, per
            Step 4's `cd "$KAIZEN_ROOT"` convention).
        purge_age_s: rows older than this many seconds are purged on
            bootstrap. Default 7 days.
    """
    path = Path(bridge_db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = _connect(path)
    try:
        con.executescript(_BRIDGE_SCHEMA)
        con.commit()
        purge_old_rows(con, cutoff_age_s=purge_age_s)
    finally:
        con.close()


def main(argv: list[str]) -> int:
    """CLI: `python3 -m scripts.bridge_db [<path>]` bootstraps the DB.

    Idempotent. Exits 0 on success.
    """
    path = argv[0] if argv else ".ai/bridge.db"
    bootstrap(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
