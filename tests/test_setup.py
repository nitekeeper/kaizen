"""Tests for scripts/setup.py — dependency verification + migration runner."""
from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import setup as setup_mod
from scripts.setup import (
    DepCheck,
    check_atelier_on_disk,
    check_gh,
    check_git,
    check_memex,
    check_python_version,
    run_setup,
    verify_all,
)


# ── Helpers ────────────────────────────────────────────────────────────────

def _ok_result(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr=stderr)


def _fail_result(stdout: str = "", stderr: str = "", code: int = 1) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=stdout, stderr=stderr)


def _all_present(monkeypatch, atelier_root: Path) -> None:
    """Patch the world so every dep check passes."""
    def fake_which(name: str):
        return f"/usr/bin/{name}" if name in {"git", "gh", "memex"} else None

    def fake_run(cmd, *args, **kwargs):
        exe = cmd[0]
        if exe == "git":
            return _ok_result(stdout="git version 2.42.0\n")
        if exe == "gh":
            return _ok_result(stdout="Logged in to github.com account nitekeeper\n")
        if exe == "memex":
            return _ok_result(stdout="memex 1.1.2\n")
        return _ok_result()

    monkeypatch.setattr(setup_mod.shutil, "which", fake_which)
    monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(setup_mod, "_find_atelier_root", lambda: atelier_root)


# ── Individual check tests ─────────────────────────────────────────────────

class TestCheckGit:
    def test_present(self, monkeypatch):
        monkeypatch.setattr(setup_mod.shutil, "which", lambda n: "/usr/bin/git")
        monkeypatch.setattr(
            setup_mod.subprocess, "run",
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
            setup_mod.subprocess, "run", lambda *a, **k: _fail_result(code=127),
        )
        c = check_git()
        assert c.ok is False


class TestCheckGh:
    def test_authenticated(self, monkeypatch):
        monkeypatch.setattr(setup_mod.shutil, "which", lambda n: "/usr/bin/gh")
        monkeypatch.setattr(
            setup_mod.subprocess, "run",
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
            setup_mod.subprocess, "run",
            lambda *a, **k: _fail_result(
                stderr="You are not logged into any GitHub hosts.\n", code=1,
            ),
        )
        c = check_gh()
        assert c.ok is False
        assert "auth login" in c.fix.lower()


class TestCheckMemex:
    def test_present(self, monkeypatch):
        monkeypatch.setattr(setup_mod.shutil, "which", lambda n: "/usr/bin/memex")
        monkeypatch.setattr(
            setup_mod.subprocess, "run",
            lambda *a, **k: _ok_result(stdout="memex 1.1.2\n"),
        )
        c = check_memex()
        assert c.ok is True

    def test_missing(self, monkeypatch):
        monkeypatch.setattr(setup_mod.shutil, "which", lambda n: None)
        c = check_memex()
        assert c.ok is False
        assert "memex" in c.fix.lower()


class TestCheckPythonVersion:
    def test_supported(self):
        # The test harness already runs on Python >= 3.11, so the real check
        # should succeed without any monkeypatching.
        c = check_python_version()
        assert c.ok is True

    def test_too_old(self, monkeypatch):
        fake = SimpleNamespace(major=3, minor=9, micro=0)
        # Make comparisons work via tuple coercion in the function
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


class TestCheckAtelierOnDisk:
    def test_found(self, monkeypatch, tmp_path):
        fake_root = tmp_path / "atelier"
        (fake_root / "scripts").mkdir(parents=True)
        (fake_root / "scripts" / "migrate.py").write_text("")
        (fake_root / "scripts" / "seed_roles.py").write_text("")
        monkeypatch.setattr(setup_mod, "_find_atelier_root", lambda: fake_root)
        c = check_atelier_on_disk()
        assert c.ok is True
        assert str(fake_root) in c.detail

    def test_not_found(self, monkeypatch):
        monkeypatch.setattr(setup_mod, "_find_atelier_root", lambda: None)
        c = check_atelier_on_disk()
        assert c.ok is False
        assert "not found" in c.detail.lower()


# ── Orchestrator tests ─────────────────────────────────────────────────────

class TestRunSetup:
    def test_all_present_returns_zero_and_applies_migration(
        self, monkeypatch, tmp_path
    ):
        atelier_root = tmp_path / "atelier"
        (atelier_root / "scripts").mkdir(parents=True)
        (atelier_root / "scripts" / "migrate.py").write_text("")
        (atelier_root / "scripts" / "seed_roles.py").write_text("")
        _all_present(monkeypatch, atelier_root)

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
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            names = {r[0] for r in rows}
        finally:
            conn.close()
        for expected in {"projects", "runs", "cycles", "abandonments", "migrations"}:
            assert expected in names, f"missing table {expected!r}; got {names}"

    def test_idempotent_rerun(self, monkeypatch, tmp_path):
        atelier_root = tmp_path / "atelier"
        (atelier_root / "scripts").mkdir(parents=True)
        (atelier_root / "scripts" / "migrate.py").write_text("")
        (atelier_root / "scripts" / "seed_roles.py").write_text("")
        _all_present(monkeypatch, atelier_root)

        db_path = tmp_path / ".ai" / "memex.db"
        db_path.parent.mkdir(parents=True)
        monkeypatch.setattr(setup_mod, "DB_PATH", db_path)

        assert run_setup() == 0
        # Second run should also succeed without error
        assert run_setup() == 0

        # Migrations table should still have exactly 1 row
        conn = sqlite3.connect(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM migrations").fetchone()[0]
        finally:
            conn.close()
        assert count == 1

    def test_failure_blocks_migration(self, monkeypatch, tmp_path, capsys):
        # git missing → run_setup returns 1 and never creates the DB
        def fake_which(n):
            return None if n == "git" else f"/usr/bin/{n}"

        def fake_run(cmd, *a, **k):
            if cmd[0] == "gh":
                return _ok_result(stdout="Logged in to github.com account x\n")
            if cmd[0] == "memex":
                return _ok_result(stdout="memex 1.1.2\n")
            return _ok_result()

        atelier_root = tmp_path / "atelier"
        (atelier_root / "scripts").mkdir(parents=True)
        (atelier_root / "scripts" / "migrate.py").write_text("")
        (atelier_root / "scripts" / "seed_roles.py").write_text("")

        monkeypatch.setattr(setup_mod.shutil, "which", fake_which)
        monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(setup_mod, "_find_atelier_root", lambda: atelier_root)

        db_path = tmp_path / ".ai" / "memex.db"
        monkeypatch.setattr(setup_mod, "DB_PATH", db_path)

        rc = run_setup()
        assert rc == 1
        assert not db_path.exists()

        out = capsys.readouterr().out
        assert "[FAIL]" in out
        assert "Setup blocked" in out


class TestVerifyAll:
    def test_returns_five_checks(self, monkeypatch, tmp_path):
        atelier_root = tmp_path / "atelier"
        (atelier_root / "scripts").mkdir(parents=True)
        (atelier_root / "scripts" / "migrate.py").write_text("")
        (atelier_root / "scripts" / "seed_roles.py").write_text("")
        _all_present(monkeypatch, atelier_root)

        checks = verify_all()
        assert len(checks) == 5
        assert {c.name for c in checks} == {"git", "gh", "memex", "atelier", "python"}
        assert all(c.ok for c in checks)
