"""Tests for scripts/seed_atelier_in_clone.py."""
import sqlite3
import subprocess
from pathlib import Path

import pytest

from scripts.seed_atelier_in_clone import (
    find_atelier_root,
    ensure_wiki_dir,
    seed_atelier_schema,
    seed_atelier_roles,
    seed_all,
)


def _init_clone(tmp_path: Path) -> Path:
    clone = tmp_path / "clone"
    clone.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(clone)],
        check=True, capture_output=True,
    )
    return clone


class TestFindAtelierRoot:
    def test_returns_a_path_with_atelier_markers(self):
        root = find_atelier_root()
        assert (root / "scripts" / "migrate.py").exists()
        assert (root / "scripts" / "seed_roles.py").exists()


class TestEnsureWikiDir:
    def test_creates_wiki_dir(self, tmp_path):
        clone = _init_clone(tmp_path)
        ensure_wiki_dir(clone)
        assert (clone / ".ai" / "wiki").is_dir()

    def test_idempotent(self, tmp_path):
        clone = _init_clone(tmp_path)
        ensure_wiki_dir(clone)
        ensure_wiki_dir(clone)  # must not raise
        assert (clone / ".ai" / "wiki").is_dir()


class TestSeedSchemaAndRoles:
    def test_schema_creates_atelier_tables(self, tmp_path):
        clone = _init_clone(tmp_path)
        seed_atelier_schema(clone)
        db_path = clone / ".ai" / "memex.db"
        assert db_path.exists()
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = {r[0] for r in rows}
        finally:
            conn.close()
        # Atelier's core tables must be present after migration.
        for required in ("projects", "agents", "roles"):
            assert required in table_names, f"Missing table {required!r}; got {sorted(table_names)}"

    def test_roles_populated_with_at_least_60_records(self, tmp_path):
        clone = _init_clone(tmp_path)
        seed_atelier_schema(clone)
        seed_atelier_roles(clone)
        db_path = clone / ".ai" / "memex.db"
        conn = sqlite3.connect(str(db_path))
        try:
            (count,) = conn.execute("SELECT COUNT(*) FROM roles").fetchone()
        finally:
            conn.close()
        assert count >= 60, f"Expected at least 60 roles, got {count}"

    def test_seed_all_runs_full_sequence(self, tmp_path):
        clone = _init_clone(tmp_path)
        seed_all(clone)
        # Wiki dir present
        assert (clone / ".ai" / "wiki").is_dir()
        # DB present with tables + roles
        db_path = clone / ".ai" / "memex.db"
        assert db_path.exists()
        conn = sqlite3.connect(str(db_path))
        try:
            (count,) = conn.execute("SELECT COUNT(*) FROM roles").fetchone()
        finally:
            conn.close()
        assert count >= 60
