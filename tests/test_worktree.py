"""Tests for scripts/worktree.py — merge_back() error paths and CLI translation."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.worktree import (
    classify_status,
    detect_worktree,
    get_current_branch,
    merge_back,
)

# ── helpers ────────────────────────────────────────────────────────────────


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _make_repo(path: Path, branch: str = "main") -> None:
    """Initialise a git repo with one commit."""
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", branch], path)
    _git(["config", "user.email", "test@test.com"], path)
    _git(["config", "user.name", "Test User"], path)
    (path / "README.md").write_text("# test\n")
    _git(["add", "."], path)
    _git(["commit", "-m", "initial"], path)


# ── classify_status ────────────────────────────────────────────────────────


class TestClassifyStatus:
    def test_empty_status(self):
        dirty, claude, other = classify_status("")
        assert dirty == [] and claude == [] and other == []

    def test_dirty_tracked_files(self):
        dirty, claude, other = classify_status(" M scripts/foo.py\n")
        assert dirty == [" M scripts/foo.py"]
        assert claude == [] and other == []

    def test_untracked_claude_separated(self):
        dirty, claude, other = classify_status("?? .claude/settings.json\n?? src/new.py\n")
        assert claude == ["?? .claude/settings.json"]
        assert other == ["?? src/new.py"]
        assert dirty == []


# ── detect_worktree / get_current_branch ──────────────────────────────────


class TestDetectWorktree:
    def test_main_worktree_not_linked(self, tmp_path):
        repo = tmp_path / "repo"
        _make_repo(repo)
        is_wt, _ = detect_worktree(repo)
        assert is_wt is False

    def test_linked_worktree_detected(self, tmp_path):
        repo = tmp_path / "repo"
        _make_repo(repo)
        wt_path = tmp_path / "linked"
        _git(["worktree", "add", str(wt_path), "-b", "feature/x"], repo)
        is_wt, _ = detect_worktree(wt_path)
        assert is_wt is True


class TestGetCurrentBranch:
    def test_returns_branch_name(self, tmp_path):
        repo = tmp_path / "repo"
        _make_repo(repo)
        assert get_current_branch(repo) == "main"


# ── merge_back() RuntimeError error paths ─────────────────────────────────


class TestMergeBackRaisesRuntimeError:
    """Each error path in merge_back() must raise RuntimeError, not SystemExit."""

    def _linked_worktree(self, tmp_path: Path):
        """Return (main_repo, linked_worktree_path)."""
        repo = tmp_path / "repo"
        _make_repo(repo)
        wt = tmp_path / "linked"
        _git(["worktree", "add", str(wt), "-b", "feature/task-x"], repo)
        return repo, wt

    def test_detached_head_raises_runtime_error(self, tmp_path):
        """merge_back() raises RuntimeError when the worktree is in detached HEAD."""
        _repo, wt = self._linked_worktree(tmp_path)
        # Detach HEAD in the linked worktree
        _git(["checkout", "--detach"], wt)
        with pytest.raises(RuntimeError, match="detached HEAD"):
            merge_back(wt)

    def test_main_not_on_base_branch_raises_runtime_error(self, tmp_path):
        """merge_back() raises RuntimeError when main workspace is on the wrong branch."""
        from unittest.mock import patch

        repo, wt = self._linked_worktree(tmp_path)
        # parse_main_worktree reports "main" as base; mock get_current_branch so
        # main_path appears to be on a different branch.
        original = __import__(
            "scripts.worktree", fromlist=["get_current_branch"]
        ).get_current_branch

        def _mock_branch(cwd):
            if cwd == repo:
                return "some-other-branch"
            return original(cwd)

        with (
            patch("scripts.worktree.get_current_branch", side_effect=_mock_branch),
            pytest.raises(RuntimeError, match="Main workspace is on"),
        ):
            merge_back(wt)

    def test_dirty_main_raises_runtime_error(self, tmp_path):
        """merge_back() raises RuntimeError when main workspace has uncommitted changes."""
        repo, wt = self._linked_worktree(tmp_path)
        # Dirty main workspace with a tracked-file modification
        (repo / "README.md").write_text("dirty\n")
        with pytest.raises(RuntimeError, match="uncommitted changes"):
            merge_back(wt)

    def test_merge_conflict_raises_runtime_error(self, tmp_path):
        """merge_back() raises RuntimeError on merge conflict."""
        repo, wt = self._linked_worktree(tmp_path)
        # Create conflicting change on the worktree branch
        (wt / "README.md").write_text("worktree version\n")
        _git(["add", "."], wt)
        _git(["commit", "-m", "wt change"], wt)
        # Create conflicting change on main
        (repo / "README.md").write_text("main version\n")
        _git(["add", "."], repo)
        _git(["commit", "-m", "main change"], repo)
        with pytest.raises(RuntimeError, match="CONFLICT"):
            merge_back(wt)


# ── CLI translation for worktree.py ────────────────────────────────────────


class TestWorktreeCLI:
    """CLI guard must convert RuntimeError → exit 1 + stderr message."""

    def _repo_root(self) -> Path:
        return Path(__file__).parent.parent

    def test_merge_back_cli_exits_0_in_non_worktree(self, tmp_path):
        """Running merge-back from main worktree exits 0 (early return — not an error)."""
        repo = tmp_path / "repo"
        _make_repo(repo)
        script = self._repo_root() / "scripts" / "worktree.py"
        result = subprocess.run(
            [sys.executable, str(script), "merge-back"],
            capture_output=True,
            text=True,
            cwd=str(repo),
            env={**__import__("os").environ, "PYTHONPATH": str(self._repo_root())},
        )
        # Not in a linked worktree → should exit 0 with informational message
        assert result.returncode == 0

    def test_unknown_command_exits_1_with_stderr(self, tmp_path):
        """Unknown CLI command exits 1 with usage on stderr."""
        result = subprocess.run(
            [sys.executable, "scripts/worktree.py", "bad-command"],
            capture_output=True,
            text=True,
            cwd=str(self._repo_root()),
        )
        assert result.returncode == 1
        assert "merge-back" in result.stderr

    def test_merge_back_cli_exits_1_on_detached_head(self, tmp_path):
        """CLI exits 1 and writes to stderr when merge_back raises RuntimeError."""
        repo = tmp_path / "repo"
        _make_repo(repo)
        wt = tmp_path / "linked"
        _git(["worktree", "add", str(wt), "-b", "feature/x"], repo)
        _git(["checkout", "--detach"], wt)
        result = subprocess.run(
            [sys.executable, str(self._repo_root() / "scripts" / "worktree.py"), "merge-back"],
            capture_output=True,
            text=True,
            cwd=str(wt),
            env={**__import__("os").environ, "PYTHONPATH": str(self._repo_root())},
        )
        assert result.returncode == 1
        assert result.stderr.strip() != ""
