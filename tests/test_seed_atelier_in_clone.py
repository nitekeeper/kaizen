"""Tests for scripts/seed_atelier_in_clone.py."""
import sqlite3
import subprocess
from pathlib import Path

import pytest

from scripts.seed_atelier_in_clone import (
    _atelier_env,
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


class TestAtelierEnv:
    def test_atelier_env_does_not_forward_secrets(self, monkeypatch):
        """_atelier_env must NOT forward ambient credentials to atelier subprocesses."""
        # Set both safe vars and obvious credentials
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setenv("HOME", "/home/testuser")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "super-secret-value")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-super-secret")
        monkeypatch.setenv("GH_TOKEN", "ghp_super-secret")
        monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "ghp_super-secret-2")
        env = _atelier_env(Path("/fake/atelier/root"))
        # Secrets must NOT be present
        assert "AWS_SECRET_ACCESS_KEY" not in env, "AWS secret leaked into subprocess env"
        assert "ANTHROPIC_API_KEY" not in env, "Anthropic key leaked into subprocess env"
        assert "GH_TOKEN" not in env, "gh token leaked into subprocess env"
        assert "GITHUB_PERSONAL_ACCESS_TOKEN" not in env, "github PAT leaked into subprocess env"
        # Safe vars must be present
        assert env.get("PATH") == "/usr/bin:/bin"
        assert env.get("HOME") == "/home/testuser"
        assert env.get("PYTHONPATH") == "/fake/atelier/root"


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
