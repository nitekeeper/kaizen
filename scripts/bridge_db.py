"""Ad-hoc bootstrap for `.ai/bridge.db`.

The bridge DB is per-machine and ephemeral relative to a single
`/kaizen:improve --mode team` invocation. It does NOT participate in
`scripts/migrate.py` — the migrations directory is reserved for
`.ai/memex.db` (kaizen's primary state DB). This module owns the bridge
DB's lifecycle: create-on-demand, idempotent re-bootstrap.

Per the python-cc-tool-bridge design (Rev 4, Decision D2):

  - Three tables: bridge_requests, bridge_heartbeat, python_heartbeat.
  - CREATE TABLE IF NOT EXISTS for every statement so bootstrap() is
    safe to call repeatedly.
  - PRAGMA journal_mode=WAL and PRAGMA busy_timeout=5000 set at every
    connection open (PRAGMAs are connection-scoped in SQLite).
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


def bootstrap(bridge_db_path: str | Path = ".ai/bridge.db") -> None:
    """Create the bridge DB (and parent dir) if absent; idempotent.

    Safe to call repeatedly — every statement in `_BRIDGE_SCHEMA` uses
    CREATE TABLE IF NOT EXISTS, so a partially-bootstrapped DB
    self-heals. Sets PRAGMA journal_mode=WAL and PRAGMA
    busy_timeout=5000 on the connection used to apply the schema.

    Args:
        bridge_db_path: filesystem path to the bridge DB. Default
            `.ai/bridge.db` (relative to the kaizen repo root, per
            Step 4's `cd "$KAIZEN_ROOT"` convention).

    TODO(follow-up): cross-run row purge. The bridge DB is described as
    "ephemeral per run" but bootstrap() currently never DELETEs rows.
    Across many runs, stale bridge_requests / bridge_heartbeat /
    python_heartbeat rows accumulate. Cross-run cleanup semantics need
    design discussion (purge on bootstrap? on finalize_run? by age? by
    run_id?) — see project-session-resume-2026-05-23.md. Deferred per
    review round 1 (m3).
    """
    path = Path(bridge_db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = _connect(path)
    try:
        con.executescript(_BRIDGE_SCHEMA)
        con.commit()
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
