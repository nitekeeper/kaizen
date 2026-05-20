"""Run a configurable test command inside a clone and parse pass counts."""
from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path


def run_tests_in_clone(clone_dir: Path, test_command: str) -> tuple[bool, int]:
    """Run test_command in clone_dir. Returns (all_passed, test_count).

    test_command is split with shlex.split (POSIX rules) and executed as a
    subprocess with cwd=clone_dir.

    NOTE: Pass-count uses regex r"={3,}\\s+(\\d+) passed" to match pytest's
    summary separator line (e.g. "=== 5 passed in 0.12s ===").
    Other test runners (npm, cargo, go test) produce different output and will
    yield count=0 even when passing. When kaizen supports non-pytest runners,
    add per-runner parsers here.
    """
    argv = shlex.split(test_command, posix=(sys.platform != "win32"))
    result = subprocess.run(
        argv,
        cwd=clone_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    count = 0
    for line in (result.stdout or "").splitlines():
        m = re.search(r"={3,}\s+(\d+) passed", line)
        if m:
            count = int(m.group(1))
            break
    return result.returncode == 0, count


if __name__ == "__main__":
    # Usage: python3 scripts/test_runner.py <clone_dir> <test_command>
    if len(sys.argv) < 3:
        print(
            "Usage: python3 scripts/test_runner.py <clone_dir> <test_command>",
            file=sys.stderr,
        )
        sys.exit(1)
    clone = Path(sys.argv[1])
    cmd = sys.argv[2]
    passed, count = run_tests_in_clone(clone, cmd)
    print(f"TESTS_PASSED={count}")
    if not passed:
        sys.exit(1)
