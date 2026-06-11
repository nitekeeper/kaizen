"""Tests for scripts/git_utils.py — enriched git failure reporting.

The push critical path (scripts/run.py) persists ``str(exc)`` — so the
string rendering of a failed git command must carry the argv, exit code,
cwd, and the stderr tail, not just "returned non-zero exit status N".
"""

from __future__ import annotations

import subprocess

import pytest

from scripts.git_utils import GitCommandError, git


@pytest.fixture
def repo(tmp_path):
    subprocess.run(
        ["git", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    return tmp_path


def test_failing_command_str_carries_context(repo):
    """Iron-Law test: str() of a failing git() call must include the offending
    ref name, the exit code, the cwd, and a stderr substring (e.g. 'fatal')."""
    bad_ref = "no-such-ref-kaizen-xyz"
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        git(["rev-parse", "--verify", bad_ref], cwd=repo)
    msg = str(excinfo.value)
    assert bad_ref in msg
    assert str(excinfo.value.returncode) in msg
    # Pre-fix these two are absent from str(CalledProcessError):
    assert "fatal" in msg.lower()
    assert str(repo) in msg


def test_error_is_calledprocesserror_instance(repo):
    """internal/clone-target/SKILL.md documents CalledProcessError — the
    enriched error must remain isinstance-compatible."""
    with pytest.raises(GitCommandError) as excinfo:
        git(["rev-parse", "--verify", "nope"], cwd=repo)
    assert isinstance(excinfo.value, subprocess.CalledProcessError)
    assert excinfo.value.cwd == repo


def test_check_false_does_not_raise(repo):
    result = git(["rev-parse", "--verify", "nope"], cwd=repo, check=False)
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode != 0
    assert "fatal" in (result.stderr or "").lower()


def test_success_returns_completed_process_with_text_capture(repo):
    result = git(["status", "--porcelain"], cwd=repo)
    assert result.returncode == 0
    assert isinstance(result, subprocess.CompletedProcess)
    assert isinstance(result.stdout, str)  # text + capture semantics preserved
