"""Pin the CI workflow's dependency installs to requirements.txt.

requirements.txt pins ``pytest>=9.0.3,<10`` but the tests job in
``.github/workflows/ci.yml`` ran a bare ``pip install pytest`` — so CI could
silently test against a pytest major the repo does not support. These tests
read the workflow TEXTUALLY (no YAML dependency) and pin the fix.

Scope is the pytest install only — ruff / bandit / pip-audit installs are
deliberately untouched (they are tool gates, not runtime/test deps).
"""

from __future__ import annotations

from pathlib import Path

_CI_YML = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"


def test_tests_job_installs_pinned_requirements():
    """Iron-Law (pre-fix failure): the workflow must install the pinned
    requirements file, not an unpinned pytest."""
    text = _CI_YML.read_text(encoding="utf-8")
    assert "pip install -r requirements.txt" in text, (
        "ci.yml must install test deps via `pip install -r requirements.txt` "
        "so CI honors the pytest pin in requirements.txt"
    )


def test_no_run_line_is_bare_pip_install_pytest():
    """Iron-Law (pre-fix failure): no `run:` line may be a bare unpinned
    `pip install pytest`."""
    for raw in _CI_YML.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line.startswith("run:"):
            continue
        cmd = line[len("run:") :].strip()
        assert cmd != "pip install pytest", (
            "ci.yml runs a bare unpinned `pip install pytest`; use "
            "`pip install -r requirements.txt` instead"
        )
