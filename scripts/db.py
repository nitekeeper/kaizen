import json
import os
import sqlite3
from collections.abc import Iterable

# Columns in the `abandonments` table that are JSON-serialised to TEXT in
# SQLite and must be deserialised to Python list/dict when read back.
# Owned here (not in scripts/abandonment.py) so every reader of the
# abandonments table — scripts/abandonment.py, scripts/pr.py, future
# reporting tooling — uses the same contract without import cycles.
ABANDONMENT_JSON_COLUMNS: tuple[str, ...] = (
    "unresolved_findings",
    "reviewer_attribution",
)


def row_to_dict_with_json(
    row,
    cols: Iterable[str],
    json_columns: Iterable[str] = (),
) -> dict:
    """Convert a sqlite3 row + column list to a dict, JSON-decoding select columns.

    For each column in `json_columns`: if the raw value is not None, run
    `json.loads` on it; if None, leave None. Columns not in `json_columns`
    pass through unchanged.

    Centralised so every reader of a JSON-bearing table (today: abandonments)
    has the same deserialisation contract. Callers that don't need JSON
    decoding can omit `json_columns` to get plain `dict(zip(...))` behaviour.
    """
    out = dict(zip(cols, row, strict=False))
    json_set = set(json_columns)
    for key in json_set:
        if key in out and out[key] is not None:
            out[key] = json.loads(out[key])
    return out


def get_connection(db_path: str) -> sqlite3.Connection:
    path = db_path if db_path == ":memory:" else str(db_path)
    if path != ":memory:" and not os.path.exists(path):
        # Create the file with restricted permissions before SQLite opens it.
        # This prevents a race window where the file exists world-readable.
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
