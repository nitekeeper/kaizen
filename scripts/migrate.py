from datetime import UTC, datetime
from pathlib import Path

from scripts.db import get_connection

MIGRATIONS_DIR: Path = Path(__file__).parent.parent / "migrations"


def apply_migrations(db_path: str, migrations_dir: Path) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS migrations (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                filename   TEXT NOT NULL UNIQUE,
                applied_at TEXT NOT NULL
            )
        """)
        conn.commit()

        applied = {row[0] for row in conn.execute("SELECT filename FROM migrations")}

        for migration_file in sorted(migrations_dir.glob("*.sql")):
            if migration_file.name in applied:
                continue
            sql = migration_file.read_text()
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO migrations (filename, applied_at) VALUES (?, ?)",
                (migration_file.name, datetime.now(UTC).isoformat()),
            )
            conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    import sys

    db_path = sys.argv[1] if len(sys.argv) > 1 else ".ai/memex.db"
    apply_migrations(db_path, MIGRATIONS_DIR)
    print(f"Migrations applied to {db_path}")
