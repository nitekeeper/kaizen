"""Bridge response writer with sqlite3 parameter binding.

This is the SOLE write path for `bridge_requests.response_json` /
`bridge_requests.error_text`. The orchestrating Claude session (S1)
invokes this helper from Bash for every queued row it has serviced;
agent-authored prose (which may contain quotes, newlines, SQL syntax,
or shell metacharacters) flows through `?` placeholders and CANNOT
escape into SQL or shell.

Invocation (the only form the SKILL prose tells Claude to use) — the
body is first written to a temp file via the Write tool (NO shell
interpolation of agent prose; a single-quoted apostrophe would break
out of a shell string), then fed to this helper on stdin::

    python3 scripts/bridge_write.py --row-id <row_id> --status ready \\
        < .ai/bridge_response_<row_id>.json
    python3 scripts/bridge_write.py --row-id <row_id> --status error \\
        < .ai/bridge_response_<row_id>.txt

Behaviour:
  * `--status` is gated by argparse `choices=("ready","error")`; the only
    interpolated string in the UPDATE statement comes from this trusted
    enum (selecting between `response_json` and `error_text`).
  * Validates that the target row exists and is currently in
    `status='pending'`. Refuses to write twice (exit code 4).
  * On `--status ready`, validates the stdin body parses as JSON.
  * Sets `PRAGMA journal_mode=WAL` AND `PRAGMA busy_timeout=5000` on
    every connection open (connection-scoped in SQLite).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bridge_write")
    ap.add_argument("--row-id", type=int, required=True, dest="row_id")
    # The `choices` enum is load-bearing: it is the ONLY value used in
    # the `f"UPDATE ... SET {col} = ?"` interpolation below. Agent-
    # authored prose never reaches that interpolation.
    ap.add_argument("--status", choices=("ready", "error"), required=True)
    ap.add_argument("--bridge-db", default=".ai/bridge.db", dest="bridge_db")
    args = ap.parse_args(argv)

    # Raw stdin — written verbatim into the column via parameter binding.
    # Never eval'd, never split, never re-quoted.
    body = sys.stdin.read()

    if args.status == "ready":
        # On the success path the body MUST be a JSON object per the
        # response_json contract (team_create→{"team_id":...},
        # send_message→{"response":...}, team_delete→{}, etc.). Reject
        # malformed payloads BEFORE writing.
        try:
            json.loads(body)
        except json.JSONDecodeError as e:
            print(
                f"bridge_write: response body is not valid JSON: {e}",
                file=sys.stderr,
            )
            return 2
        col = "response_json"
    else:
        # The error path accepts free-form text (a one-line diagnostic).
        col = "error_text"

    con = sqlite3.connect(args.bridge_db)
    try:
        # PRAGMAs are connection-scoped; re-apply on every open (the
        # bridge_db.bootstrap() helper sets them on its own connection,
        # which closes by the time we get here).
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA busy_timeout = 5000;")

        cur = con.execute(
            "SELECT status FROM bridge_requests WHERE id = ?",
            (args.row_id,),
        )
        row = cur.fetchone()
        if row is None:
            print(
                f"bridge_write: row {args.row_id} does not exist",
                file=sys.stderr,
            )
            return 3
        if row[0] != "pending":
            print(
                f"bridge_write: row {args.row_id} is in status={row[0]!r}, refusing to write twice",
                file=sys.stderr,
            )
            return 4

        # The only interpolation is `col`, which is selected from a
        # closed set of two trusted column names (`response_json` /
        # `error_text`) gated by the `--status` argparse enum. Every
        # value supplied by S1's invocation (row id, status, payload)
        # flows through `?` placeholders.
        con.execute(
            f"UPDATE bridge_requests SET {col} = ?, status = ?, "  # nosec B608
            "completed_at = datetime('now') WHERE id = ? AND status = 'pending'",
            (body, args.status, args.row_id),
        )
        con.commit()
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
