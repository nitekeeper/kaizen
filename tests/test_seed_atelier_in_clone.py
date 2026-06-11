"""Tests for scripts/seed_atelier_in_clone.py."""

import sqlite3
import subprocess
from pathlib import Path

import pytest

from scripts.seed_atelier_in_clone import (
    _AGORA_ATELIER,
    _atelier_env,
    ensure_wiki_dir,
    find_atelier_root,
    seed_all,
    seed_atelier_roles,
    seed_atelier_schema,
)

_ATELIER_PRESENT = _AGORA_ATELIER.is_dir()
_SKIP_NO_ATELIER = pytest.mark.skipif(
    not _ATELIER_PRESENT,
    reason="Atelier plugin cache not present at ~/.claude/plugins/cache/agora/atelier/",
)


def _init_clone(tmp_path: Path) -> Path:
    clone = tmp_path / "clone"
    clone.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(clone)],
        check=True,
        capture_output=True,
    )
    return clone


@_SKIP_NO_ATELIER
class TestFindAtelierRoot:
    def test_returns_a_path_with_atelier_markers(self):
        root = find_atelier_root()
        assert (root / "scripts" / "migrate.py").exists()
        assert (root / "scripts" / "seed_roles.py").exists()


class TestFindAtelierRootVersionOrdering:
    def _mk_atelier_version(self, cache: Path, name: str) -> None:
        d = cache / name / "scripts"
        d.mkdir(parents=True)
        (d / "migrate.py").write_text("# marker\n")
        (d / "seed_roles.py").write_text("# marker\n")

    def test_find_atelier_root_prefers_numeric_newest(self, tmp_path, monkeypatch):
        """REGRESSION: '2.9.0' > '2.10.0' lexicographically — the resolver must
        compare numerically and pick 2.10.0."""
        import scripts.seed_atelier_in_clone as mod

        cache = tmp_path / "atelier"
        cache.mkdir()
        self._mk_atelier_version(cache, "2.9.0")
        self._mk_atelier_version(cache, "2.10.0")
        monkeypatch.setattr(mod, "_AGORA_ATELIER", cache)
        assert mod.find_atelier_root().name == "2.10.0"

    def test_raises_when_cache_dir_missing(self, tmp_path, monkeypatch):
        import scripts.seed_atelier_in_clone as mod

        monkeypatch.setattr(mod, "_AGORA_ATELIER", tmp_path / "nope")
        with pytest.raises(RuntimeError, match="plugin cache not found"):
            mod.find_atelier_root()

    def test_raises_when_no_valid_install(self, tmp_path, monkeypatch):
        import scripts.seed_atelier_in_clone as mod

        cache = tmp_path / "atelier"
        (cache / "2.9.0").mkdir(parents=True)  # no marker files
        monkeypatch.setattr(mod, "_AGORA_ATELIER", cache)
        with pytest.raises(RuntimeError, match="No valid Atelier installation"):
            mod.find_atelier_root()


class TestStandaloneInvocation:
    def test_script_imports_without_pythonpath(self):
        """REGRESSION: internal/clone-target/SKILL.md invokes the script as bare
        `python3 scripts/seed_atelier_in_clone.py <clone-dir>` (no PYTHONPATH).
        The plugin_cache import must not break that entrypoint — a no-arg run
        must reach the usage message (exit 1), not die on ImportError."""
        import os
        import sys

        repo_root = Path(__file__).resolve().parent.parent
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        proc = subprocess.run(
            [sys.executable, "scripts/seed_atelier_in_clone.py"],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 1, proc.stderr
        assert "Usage:" in proc.stderr
        assert "Traceback" not in proc.stderr


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


class TestCopyRolesFromAtelier:
    def test_copy_roles_raises_if_memex_registry_missing(self, tmp_path, monkeypatch):
        """If ~/.memex/registry.json is absent, _copy_roles_agents_from_atelier must raise."""
        from scripts.seed_atelier_in_clone import _copy_roles_agents_from_atelier

        # Redirect HOME so registry path doesn't exist
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        clone_dir = tmp_path / "clone"
        (clone_dir / ".ai").mkdir(parents=True)
        # Minimal memex.db with roles/agents tables (won't be reached)
        import sqlite3 as _sq3

        c = _sq3.connect(str(clone_dir / ".ai" / "memex.db"))
        c.execute(
            "CREATE TABLE roles (id INTEGER PRIMARY KEY, name TEXT, description TEXT, created_at TEXT, updated_at TEXT)"
        )
        c.execute(
            "CREATE TABLE agents (id TEXT PRIMARY KEY, name TEXT, role_id INTEGER, profile TEXT, created_at TEXT, updated_at TEXT)"
        )
        c.close()
        with pytest.raises(RuntimeError, match="Memex registry not found"):
            _copy_roles_agents_from_atelier(clone_dir)


@_SKIP_NO_ATELIER
class TestSeedSchemaAndRoles:
    def test_schema_creates_atelier_tables(self, tmp_path):
        clone = _init_clone(tmp_path)
        seed_atelier_schema(clone)
        db_path = clone / ".ai" / "memex.db"
        assert db_path.exists()
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
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
