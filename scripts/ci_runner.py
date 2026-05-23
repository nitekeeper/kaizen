"""Run target-repo CI checks locally in a clone (tests + ruff if configured).

Separate from scripts/test_runner.py — that module's contract is "run the test
command and count passed tests"; this module's contract is "run every CI check
the target repo defines and return per-check results."

Currently supports: pytest (via test_command) + ruff check + ruff format. Targets
that use flake8/mypy/black are NOT auto-detected — a warning is logged when no
known lint config is found, and the cycle's Phase 5b agent must verify whether
the target's actual CI has other checks not mirrored here.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path


def _has_ruff_config(clone_dir: Path) -> bool:
    """Return True if the target repo opts in to ruff."""
    if (clone_dir / "ruff.toml").exists():
        return True
    pyproject = clone_dir / "pyproject.toml"
    if pyproject.exists():
        try:
            content = pyproject.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        return "[tool.ruff]" in content or "[tool.ruff." in content
    return False


def run_ci_checks(
    clone_dir: Path,
    test_command: str,
) -> tuple[bool, dict[str, tuple[bool, str]]]:
    """Run the test suite plus ruff checks (if the target repo configures ruff).

    Returns:
        all_passed: True only if every check returned exit 0.
        results:    mapping of check_name -> (passed: bool, output: str).
                    Keys always present: "tests".
                    Keys present when ruff config detected:
                        "ruff_check", "ruff_format".
                    When no known lint config is found, results includes the
                    synthetic key "lint_warning" with passed=True and an
                    output string explaining that lint was skipped.

    See internal/cycle/SKILL.md Phase 5b for the routing rules that consume
    the per-check dict.
    """
    results: dict[str, tuple[bool, str]] = {}

    # Always run the project's test command.
    argv = shlex.split(test_command, posix=(sys.platform != "win32"))
    proc = subprocess.run(
        argv,
        cwd=clone_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    results["tests"] = (proc.returncode == 0, proc.stdout + proc.stderr)

    if _has_ruff_config(clone_dir):
        for name, argv_ruff in [
            ("ruff_check", ["ruff", "check", "."]),
            ("ruff_format", ["ruff", "format", "--check", "."]),
        ]:
            try:
                r = subprocess.run(
                    argv_ruff,
                    cwd=clone_dir,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
                results[name] = (r.returncode == 0, r.stdout + r.stderr)
            except FileNotFoundError:
                # Safety F1.3: ruff binary absent. Fail loudly with a named
                # error rather than crashing the cycle. Caller should install
                # ruff (or stop opting in via pyproject.toml [tool.ruff]).
                results[name] = (
                    False,
                    f"ruff binary not found on PATH — install ruff to enable "
                    f"the '{name}' CI mirror check (or remove [tool.ruff] "
                    f"from the target's pyproject.toml to skip lint).",
                )
    else:
        results["lint_warning"] = (
            True,
            "No ruff config detected in the target repo "
            "(ruff.toml absent and pyproject.toml has no [tool.ruff] section). "
            "Lint checks were skipped. If the target's actual CI runs flake8, "
            "mypy, black, or another linter, this cycle may report green "
            "prematurely — Phase 5b agents must verify against the target's "
            ".github/workflows/ before relying on local CI mirror.",
        )

    all_passed = all(passed for passed, _ in results.values())
    return all_passed, results
