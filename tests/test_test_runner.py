"""Tests for scripts/test_runner.py — parameterized test command runner."""
import sys
from pathlib import Path

import pytest

from scripts.test_runner import run_tests_in_clone
from scripts.clone import clone_repo


# Default pytest invocation used by the live project config.
# `pytest` is expected on PATH; sys.executable cannot be embedded in the
# command string because shlex.split mangles Windows backslash paths.
_DEFAULT_PYTEST = f"{sys.executable} -m pytest -v --tb=short"


class TestRunTestsInClone:
    def test_passing_tests_returns_true_and_count(self, tmp_path, bare_remote, source_repo):
        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest)
        passed, count = run_tests_in_clone(dest, _DEFAULT_PYTEST)
        assert passed is True
        assert count == 1

    def test_failing_tests_returns_false(self, tmp_path, bare_remote, source_repo):
        from tests.conftest import _git
        # Push a failing test to the remote
        (source_repo / "tests" / "test_fail.py").write_text("def test_fail(): assert False\n")
        _git(["add", "."], source_repo)
        _git(["commit", "-m", "add failing test"], source_repo)
        _git(["push"], source_repo)
        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest)
        passed, _count = run_tests_in_clone(dest, _DEFAULT_PYTEST)
        assert passed is False

    def test_custom_test_command_string_parses_correctly(self, tmp_path, bare_remote, source_repo):
        """A different pytest invocation string is split by shlex and run successfully."""
        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest)
        # Custom command string with extra args; shlex.split must handle it.
        cmd = f"{sys.executable} -m pytest -v --tb=short"
        passed, count = run_tests_in_clone(dest, cmd)
        assert passed is True
        assert count == 1
