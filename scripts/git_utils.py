"""Shared git subprocess helper used by cycle_git, clone, and worktree modules."""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitCommandError(subprocess.CalledProcessError):
    """CalledProcessError enriched with cwd and a stderr tail in ``str()``.

    The push critical path (scripts/run.py) persists ``str(exc)`` — the
    default CalledProcessError rendering drops stderr entirely, which left
    failures like auth/bad-ref/network undiagnosable from the run row.
    Stays a CalledProcessError subclass so the documented isinstance
    contract (internal/clone-target/SKILL.md) holds unchanged.
    """

    _STDERR_TAIL_LINES = 5

    def __init__(self, returncode, cmd, cwd=None, output=None, stderr=None):
        super().__init__(returncode, cmd, output=output, stderr=stderr)
        self.cwd = cwd

    def __str__(self) -> str:
        parts = [f"git command {self.cmd!r} exited with code {self.returncode}"]
        if self.cwd is not None:
            parts.append(f"(cwd: {self.cwd})")
        tail = (self.stderr or "").strip()
        if tail:
            lines = tail.splitlines()[-self._STDERR_TAIL_LINES :]
            parts.append("stderr: " + " | ".join(line.strip() for line in lines))
        return " ".join(parts)


def git(args: list[str], cwd: Path, check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        **kwargs,
    )
    if check and result.returncode != 0:
        raise GitCommandError(
            result.returncode,
            result.args,
            cwd=cwd,
            output=result.stdout,
            stderr=result.stderr,
        )
    return result
