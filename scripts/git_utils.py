"""Shared git subprocess helper used by cycle_git, clone, and worktree modules."""

from __future__ import annotations

import subprocess
from pathlib import Path


def git(args: list[str], cwd: Path, check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=check,
        **kwargs,
    )
