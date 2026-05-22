import os
import sqlite3


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
