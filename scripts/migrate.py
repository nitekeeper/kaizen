import contextlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from scripts.db import get_connection

MIGRATIONS_DIR: Path = Path(__file__).parent.parent / "migrations"

# Transaction-control statements stripped from migration scripts: the outer
# BEGIN IMMEDIATE in apply_migrations subsumes the self-wrapping in
# migrations 003-006 (sqlite3 forbids nested BEGIN).
_TX_CONTROL = frozenset(
    {
        "BEGIN",
        "BEGIN TRANSACTION",
        "BEGIN DEFERRED",
        "BEGIN IMMEDIATE",
        "BEGIN EXCLUSIVE",
        "COMMIT",
        "COMMIT TRANSACTION",
        "END",
        "END TRANSACTION",
    }
)


def _split_sql(sql: str) -> list[str]:
    """Split a migration script into complete statements.

    Accumulates lines until ``sqlite3.complete_statement`` reports a complete
    statement (this respects strings, comments, and CREATE TRIGGER bodies).
    Any non-empty tail (e.g. trailing comments) is kept as a final chunk and
    filtered by _skippable() at execution time.
    """
    statements: list[str] = []
    buf = ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            statements.append(buf.strip())
            buf = ""
    if buf.strip():
        statements.append(buf.strip())
    return statements


def _skippable(stmt: str) -> bool:
    """True for chunks that must not be executed individually.

    Skips comment-only/empty chunks and bare BEGIN/COMMIT lines — the outer
    transaction in apply_migrations replaces the scripts' own wrapping.
    """
    lines = [ln for ln in stmt.splitlines() if not ln.lstrip().startswith("--")]
    body = " ".join(" ".join(lines).split()).strip().rstrip(";").strip().upper()
    if not body:
        return True
    return body in _TX_CONTROL


def apply_migrations(db_path: str, migrations_dir: Path) -> None:
    """Apply pending migrations, each atomically WITH its bookkeeping row.

    Historic bug: ``executescript`` implicitly commits, then a separate
    INSERT+commit recorded the migration — a crash in that window left the
    migration applied but unrecorded, wedging the DB on the next run.
    Now each migration's statements AND its bookkeeping INSERT share one
    BEGIN IMMEDIATE ... COMMIT; any failure rolls the whole migration back.
    """
    conn = get_connection(db_path)
    conn.isolation_level = None  # autocommit mode — we manage transactions explicitly
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS migrations (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                filename   TEXT NOT NULL UNIQUE,
                applied_at TEXT NOT NULL
            )
        """)

        applied = {row[0] for row in conn.execute("SELECT filename FROM migrations")}

        for migration_file in sorted(migrations_dir.glob("*.sql")):
            if migration_file.name in applied:
                continue
            statements = _split_sql(migration_file.read_text())
            conn.execute("BEGIN IMMEDIATE")
            try:
                for stmt in statements:
                    if _skippable(stmt):
                        continue
                    conn.execute(stmt)
                conn.execute(
                    "INSERT INTO migrations (filename, applied_at) VALUES (?, ?)",
                    (migration_file.name, datetime.now(UTC).isoformat()),
                )
                conn.execute("COMMIT")
            except BaseException:
                # If COMMIT itself failed there is no active transaction;
                # a bare ROLLBACK would raise and mask the original error.
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise
    finally:
        conn.close()


if __name__ == "__main__":
    import sys

    db_path = sys.argv[1] if len(sys.argv) > 1 else ".ai/memex.db"
    apply_migrations(db_path, MIGRATIONS_DIR)
    print(f"Migrations applied to {db_path}")
