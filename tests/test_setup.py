"""Tests for scripts/setup.py — dependency verification + migration runner."""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

from scripts import setup as setup_mod
from scripts.setup import (
    check_atelier,
    check_gh,
    check_git,
    check_python_version,
    run_setup,
    verify_all,
)

# ── Helpers ────────────────────────────────────────────────────────────────


def _ok_result(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr=stderr)


def _fail_result(stdout: str = "", stderr: str = "", code: int = 1) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=stdout, stderr=stderr)


def _all_present(monkeypatch, tmp_path=None) -> None:
    """Patch the world so every dep check passes."""

    def fake_which(name: str):
        return f"/usr/bin/{name}" if name in {"git", "gh"} else None

    def fake_run(cmd, *args, **kwargs):
        exe = cmd[0]
        if exe == "git":
            return _ok_result(stdout="git version 2.42.0\n")
        if exe == "gh":
            return _ok_result(stdout="Logged in to github.com account nitekeeper\n")
        return _ok_result()

    monkeypatch.setattr(setup_mod.shutil, "which", fake_which)
    monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)

    # Patch find_atelier_root to return a deterministic path without touching
    # the real plugin cache.
    fake_atelier_root = Path("/fake/atelier/v1.0.0")
    monkeypatch.setattr(setup_mod, "find_atelier_root", lambda: fake_atelier_root)


# ── Individual check tests ─────────────────────────────────────────────────


class TestCheckGit:
    def test_present(self, monkeypatch):
        monkeypatch.setattr(setup_mod.shutil, "which", lambda n: "/usr/bin/git")
        monkeypatch.setattr(
            setup_mod.subprocess,
            "run",
            lambda *a, **k: _ok_result(stdout="git version 2.42.0\n"),
        )
        c = check_git()
        assert c.ok is True
        assert "2.42" in c.detail

    def test_missing(self, monkeypatch):
        monkeypatch.setattr(setup_mod.shutil, "which", lambda n: None)
        c = check_git()
        assert c.ok is False
        assert "not found" in c.detail.lower()
        assert "install git" in c.fix.lower()

    def test_present_but_fails(self, monkeypatch):
        monkeypatch.setattr(setup_mod.shutil, "which", lambda n: "/usr/bin/git")
        monkeypatch.setattr(
            setup_mod.subprocess,
            "run",
            lambda *a, **k: _fail_result(code=127),
        )
        c = check_git()
        assert c.ok is False


class TestCheckGh:
    def test_authenticated(self, monkeypatch):
        monkeypatch.setattr(setup_mod.shutil, "which", lambda n: "/usr/bin/gh")
        monkeypatch.setattr(
            setup_mod.subprocess,
            "run",
            lambda *a, **k: _ok_result(stdout="Logged in to github.com account nitekeeper\n"),
        )
        c = check_gh()
        assert c.ok is True

    def test_not_installed(self, monkeypatch):
        monkeypatch.setattr(setup_mod.shutil, "which", lambda n: None)
        c = check_gh()
        assert c.ok is False
        assert "install" in c.fix.lower()
        assert "auth login" not in c.fix.lower()

    def test_installed_but_not_authenticated(self, monkeypatch):
        monkeypatch.setattr(setup_mod.shutil, "which", lambda n: "/usr/bin/gh")
        monkeypatch.setattr(
            setup_mod.subprocess,
            "run",
            lambda *a, **k: _fail_result(
                stderr="You are not logged into any GitHub hosts.\n",
                code=1,
            ),
        )
        c = check_gh()
        assert c.ok is False
        assert "auth login" in c.fix.lower()


class TestCheckPythonVersion:
    def test_supported(self):
        # The test harness already runs on Python >= 3.11, so the real check
        # should succeed without any monkeypatching.
        c = check_python_version()
        assert c.ok is True

    def test_too_old(self, monkeypatch):
        # Make comparisons work via tuple coercion in the function.
        # The function does: sys.version_info >= (3, 11). SimpleNamespace won't
        # support that, so wrap as a tuple-like by subclassing tuple.
        class V(tuple):
            major = 3
            minor = 9
            micro = 0

        v = V((3, 9, 0))
        monkeypatch.setattr(setup_mod.sys, "version_info", v)
        c = check_python_version()
        assert c.ok is False
        assert "3.11" in c.detail or "3.11" in c.fix


# ── Orchestrator tests ─────────────────────────────────────────────────────


class TestRunSetup:
    def test_all_present_returns_zero_and_applies_migration(self, monkeypatch, tmp_path):
        _all_present(monkeypatch)

        db_path = tmp_path / ".ai" / "memex.db"
        db_path.parent.mkdir(parents=True)
        monkeypatch.setattr(setup_mod, "DB_PATH", db_path)
        # MIGRATIONS_DIR is the real one — let the migration run for real.

        rc = run_setup()
        assert rc == 0
        assert db_path.exists()

        # Verify the 4 kaizen tables + migrations table exist
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            names = {r[0] for r in rows}
        finally:
            conn.close()
        for expected in {"projects", "runs", "cycles", "abandonments", "migrations"}:
            assert expected in names, f"missing table {expected!r}; got {names}"

    def test_idempotent_rerun(self, monkeypatch, tmp_path):
        _all_present(monkeypatch)

        db_path = tmp_path / ".ai" / "memex.db"
        db_path.parent.mkdir(parents=True)
        monkeypatch.setattr(setup_mod, "DB_PATH", db_path)

        assert run_setup() == 0
        # Second run should also succeed without error
        assert run_setup() == 0

        # Migrations table should still have exactly 6 rows (001-006)
        conn = sqlite3.connect(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM migrations").fetchone()[0]
        finally:
            conn.close()
        assert count == 6

    def test_failure_blocks_migration(self, monkeypatch, tmp_path, capsys):
        # git missing → run_setup returns 1 and never creates the DB
        def fake_which(n):
            return None if n == "git" else f"/usr/bin/{n}"

        def fake_run(cmd, *a, **k):
            if cmd[0] == "gh":
                return _ok_result(stdout="Logged in to github.com account x\n")
            return _ok_result()

        monkeypatch.setattr(setup_mod.shutil, "which", fake_which)
        monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(setup_mod, "find_atelier_root", lambda: Path("/fake/atelier"))

        db_path = tmp_path / ".ai" / "memex.db"
        monkeypatch.setattr(setup_mod, "DB_PATH", db_path)

        rc = run_setup()
        assert rc == 1
        assert not db_path.exists()

        out = capsys.readouterr().out
        assert "[FAIL]" in out
        assert "Setup blocked" in out


class TestCheckAtelier:
    def test_present(self, monkeypatch, tmp_path):
        """Happy path: find_atelier_root returns a valid path → ok=True."""
        monkeypatch.setattr(setup_mod, "find_atelier_root", lambda: tmp_path)
        c = check_atelier()
        assert c.ok is True
        assert str(tmp_path) in c.detail
        assert "atelier" in c.name.lower()

    def test_not_found_runtime_error(self, monkeypatch):
        """Sad path: find_atelier_root raises RuntimeError → ok=False with helpful detail."""

        def _raise():
            raise RuntimeError("Atelier plugin cache not found at /fake/path")

        monkeypatch.setattr(setup_mod, "find_atelier_root", _raise)
        c = check_atelier()
        assert c.ok is False
        assert "atelier" in c.detail.lower() or "cache" in c.detail.lower()
        assert "agora install atelier" in c.fix.lower()

    def test_not_found_generic_exception(self, monkeypatch):
        """Sad path: find_atelier_root raises unexpected exception → treated as not-found."""

        def _raise():
            raise OSError("permission denied")

        monkeypatch.setattr(setup_mod, "find_atelier_root", _raise)
        c = check_atelier()
        assert c.ok is False
        assert "permission denied" in c.detail.lower()
        assert "agora install atelier" in c.fix.lower()


class TestVerifyAll:
    def test_returns_four_checks(self, monkeypatch):
        _all_present(monkeypatch)

        checks = verify_all()
        assert len(checks) == 4
        assert {c.name for c in checks} == {"git", "gh", "python", "atelier"}
        assert all(c.ok for c in checks)
