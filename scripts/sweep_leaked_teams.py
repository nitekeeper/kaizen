"""JSON1 orphan-team finder + `aborted` enqueuer for the next-run sweep.

Layer 3 of the leaked-team recovery design. Invoked from
`skills/improve/SKILL.md` Step 1 to catch any `TeamCreate` that has no
matching `TeamDelete` from a previous run (e.g. because that run
crashed before `team_cycle_executor`'s finally could enqueue the
delete).

The query joins `json_extract(args_json, '$.team_id')` for `team_delete`
rows against `json_extract(response_json, '$.team_id')` for
`team_create` rows — the canonical team_id post-creation is in the
RESPONSE, not the request (`args_json` of `team_create` carries only
`name` + `members`). MINOR-JSON1-PATH inline comment below documents
the `status='ready'` contract assumption.

CLI: `python3 -m scripts.sweep_leaked_teams [--bridge-db PATH] [--enqueue-into-run RUN_ID]`.
Without `--enqueue-into-run`, prints the orphan list to stdout (one
`run_id\tteam_id` per line). With it, INSERTs a single `aborted` row
into that run's queue with `team_ids_at_risk` populated.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from scripts.bridge_db import bootstrap

# MINOR-JSON1-PATH (verbatim from the design doc): the `status='ready'`
# filter encodes the CURRENT contract that bridge_write.py only writes
# 'ready' when the session-tool call already succeeded — so a
# team_create row with status='error' never has a valid
# response_json.team_id. If a future bridge_write contract evolves to
# write team_create rows as 'error' AFTER a successful TeamCreate
# (e.g. for partial-failure reporting), this CTE will silently miss
# those orphans. Re-audit if the contract changes.
_ORPHAN_SQL = """
WITH created AS (
  SELECT run_id, id AS req_id,
         json_extract(args_json, '$.name') AS name,
         json_extract(response_json, '$.team_id') AS team_id,
         created_at
  FROM bridge_requests
  WHERE kind = 'team_create' AND status = 'ready'
    -- m1 (review round 1): scope the sweep to recent runs only.
    -- Older orphan team_ids may have been cleaned up by Anthropic-
    -- side TTL already; re-enqueuing TeamDelete on them produces
    -- futile aborted rows. 7 days is comfortably longer than any
    -- plausible kaizen run.
    AND created_at >= datetime('now', '-7 days')
),
deleted AS (
  SELECT json_extract(args_json, '$.team_id') AS team_id
  FROM bridge_requests
  WHERE kind = 'team_delete' AND status = 'ready'
)
SELECT c.run_id, c.team_id
  FROM created c
 WHERE c.team_id IS NOT NULL
   AND c.team_id NOT IN (
     SELECT team_id FROM deleted WHERE team_id IS NOT NULL
   )
"""


def _connect(db_path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA busy_timeout = 5000;")
    return con


def find_orphan_team_ids(bridge_db_path: str | Path) -> list[tuple[int, str]]:
    """Return list of (origin_run_id, team_id) tuples for orphans.

    "Origin run" is the run in which `team_create` was issued; this is
    useful when the user wants to know WHICH past run leaked the team.
    """
    bootstrap(bridge_db_path)
    con = _connect(bridge_db_path)
    try:
        cur = con.execute(_ORPHAN_SQL)
        return [(int(r[0]), str(r[1])) for r in cur.fetchall()]
    finally:
        con.close()


def enqueue_aborted_row(
    bridge_db_path: str | Path,
    run_id: int,
    orphan_team_ids: list[str],
    reason: str = "next-run sweep: orphan TeamCreate(s) from prior run(s)",
) -> int:
    """INSERT a single `aborted` row into the new run's queue.

    The orchestrating Claude session services `aborted` by calling
    `TeamDelete` on each id in `args_json['team_ids_at_risk']` — Python
    is the producer; S1 does NOT re-derive via SQL.
    """
    args_json = json.dumps({"reason": reason, "team_ids_at_risk": list(orphan_team_ids)})
    con = _connect(bridge_db_path)
    try:
        cur = con.execute(
            "INSERT INTO bridge_requests (run_id, kind, args_json, status) "
            "VALUES (?, 'aborted', ?, 'pending')",
            (run_id, args_json),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sweep_leaked_teams")
    ap.add_argument("--bridge-db", default=".ai/bridge.db", dest="bridge_db")
    ap.add_argument(
        "--enqueue-into-run",
        type=int,
        default=None,
        dest="enqueue_into_run",
        help="If set, enqueue an 'aborted' row in this run's queue carrying the orphan team_ids.",
    )
    args = ap.parse_args(argv)

    orphans = find_orphan_team_ids(args.bridge_db)
    if not orphans:
        print("sweep_leaked_teams: no orphan team_ids found", file=sys.stderr)
        return 0

    if args.enqueue_into_run is None:
        for origin_run, team_id in orphans:
            print(f"{origin_run}\t{team_id}")
        return 0

    team_ids = [tid for _, tid in orphans]
    row_id = enqueue_aborted_row(args.bridge_db, args.enqueue_into_run, team_ids)
    print(
        f"sweep_leaked_teams: enqueued aborted row id={row_id} into run "
        f"{args.enqueue_into_run} with {len(team_ids)} team_ids_at_risk",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
