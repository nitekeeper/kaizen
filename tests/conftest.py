"""Shared pytest fixtures for kaizen tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        encoding="utf-8",
    )


@pytest.fixture
def bare_remote(tmp_path):
    """Bare repo acting as origin."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    subprocess.run(
        ["git", "init", "--bare", str(remote)],
        check=True,
        capture_output=True,
    )
    return remote


@pytest.fixture
def source_repo(tmp_path, bare_remote):
    """Local repo with one passing test, pushed to bare_remote on main."""
    repo = tmp_path / "source"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "test@test.com"], repo)
    _git(["config", "user.name", "Test User"], repo)
    _git(["remote", "add", "origin", str(bare_remote)], repo)
    (repo / "README.md").write_text("# kaizen test target\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_dummy.py").write_text("def test_ok(): assert True\n")
    _git(["add", "."], repo)
    _git(["commit", "-m", "initial"], repo)
    _git(["push", "-u", "origin", "main"], repo)
    return repo


@pytest.fixture
def bare_remote_trunk(tmp_path):
    """Bare repo whose initial branch is `trunk` (non-main), seeded with one commit."""
    remote = tmp_path / "remote_trunk.git"
    remote.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "-b", "trunk", str(remote)],
        check=True,
        capture_output=True,
    )
    # Seed the bare remote by pushing from a scratch repo on `trunk`.
    seed = tmp_path / "seed_trunk"
    seed.mkdir()
    _git(["init", "-b", "trunk"], seed)
    _git(["config", "user.email", "test@test.com"], seed)
    _git(["config", "user.name", "Test User"], seed)
    _git(["remote", "add", "origin", str(remote)], seed)
    (seed / "README.md").write_text("# kaizen test target (trunk)\n")
    _git(["add", "."], seed)
    _git(["commit", "-m", "initial"], seed)
    _git(["push", "-u", "origin", "trunk"], seed)
    return remote
