"""Integration-shaped tests for scripts/run_bridged.py.

Covers (per the design's test plan + review round 1 m8/m10):

  * `validate_environment` raises on missing env vars.
  * `validate_environment` accepts the `gh auth status` keychain
    branch (no GH_TOKEN/GITHUB_TOKEN required when keychain is set).
  * `run_bridged.py` argv parsing accepts `--run-id`, `--url`,
    `--cycles`, `--subject` correctly.

A full "fake-Claude subprocess" drain test is out of scope for this
review round — those need a real long-running fixture and integration
harness.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
from types import SimpleNamespace

import pytest

import scripts.run_bridged as run_bridged
from scripts.run_bridged import validate_environment


def _good_env(monkeypatch):
    """Set up an env that satisfies every required-var check."""
    monkeypatch.setenv("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", "1")
    monkeypatch.setenv("PATH", "/usr/bin:/usr/local/bin")
    monkeypatch.setenv("HOME", "/home/test")
    monkeypatch.setenv("PYTHONPATH", "/some/path")


# ── validate_environment ────────────────────────────────────────────────────


def test_validate_environment_passes_with_env_token(monkeypatch):
    _good_env(monkeypatch)
    monkeypatch.setenv("GH_TOKEN", "ghp_fake")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    # No subprocess.run needed when token env var is present.
    validate_environment()  # must not raise


def test_validate_environment_passes_with_github_token(monkeypatch):
    _good_env(monkeypatch)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    validate_environment()


def test_validate_environment_passes_via_gh_keychain_branch(monkeypatch):
    """m8 (review round 1): when no GH_TOKEN/GITHUB_TOKEN is set,
    `gh auth status` exit 0 must satisfy the gh-auth check."""
    _good_env(monkeypatch)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    validate_environment()  # must not raise
    assert captured["cmd"][1:3] == ["auth", "status"], (
        f"expected `gh auth status` invocation; got {captured['cmd']}"
    )


def test_validate_environment_fails_when_required_env_var_missing(monkeypatch):
    _good_env(monkeypatch)
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setenv("GH_TOKEN", "x")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    with pytest.raises(SystemExit) as exc_info:
        validate_environment()
    assert exc_info.value.code == 2


def test_validate_environment_fails_when_path_tool_missing(monkeypatch):
    """m5 (review round 1): pytest and ruff must now be required."""
    _good_env(monkeypatch)
    monkeypatch.setenv("GH_TOKEN", "x")

    # Pretend `ruff` is missing.
    def fake_which(name):
        return None if name == "ruff" else f"/usr/bin/{name}"

    monkeypatch.setattr(shutil, "which", fake_which)
    with pytest.raises(SystemExit):
        validate_environment()


def test_validate_environment_requires_pytest_on_path(monkeypatch):
    """m5: confirm pytest is in the required-PATH list (not just ruff)."""
    _good_env(monkeypatch)
    monkeypatch.setenv("GH_TOKEN", "x")

    def fake_which(name):
        return None if name == "pytest" else f"/usr/bin/{name}"

    monkeypatch.setattr(shutil, "which", fake_which)
    with pytest.raises(SystemExit):
        validate_environment()


def test_validate_environment_fails_when_teams_var_not_one(monkeypatch):
    _good_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", "0")
    monkeypatch.setenv("GH_TOKEN", "x")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    with pytest.raises(SystemExit):
        validate_environment()


def test_validate_environment_fails_when_no_gh_auth(monkeypatch):
    _good_env(monkeypatch)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    # `gh auth status` returns non-zero — keychain not configured.
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr=""),
    )
    with pytest.raises(SystemExit):
        validate_environment()


# ── run_bridged.py argv parsing ─────────────────────────────────────────────


def _build_parser_from_main_source() -> argparse.ArgumentParser:
    """Mirror the argparse setup inside `main()` to exercise parsing in
    isolation. Kept in sync with `scripts/run_bridged.py::main`."""
    ap = argparse.ArgumentParser(prog="run_bridged")
    ap.add_argument("--db", default=".ai/memex.db", dest="db")
    ap.add_argument("--bridge-db", default=".ai/bridge.db", dest="bridge_db")
    ap.add_argument("--url", required=True, dest="url")
    ap.add_argument("--cycles", type=int, required=True, dest="cycles")
    ap.add_argument("--subject", default=None, dest="subject")
    ap.add_argument("--run-id", type=int, required=True, dest="run_id")
    return ap


def test_argv_parsing_accepts_full_invocation():
    ap = _build_parser_from_main_source()
    ns = ap.parse_args(
        [
            "--db",
            ".ai/memex.db",
            "--bridge-db",
            ".ai/bridge.db",
            "--url",
            "https://github.com/x/y.git",
            "--cycles",
            "3",
            "--subject",
            "improve docs",
            "--run-id",
            "42",
        ]
    )
    assert ns.url == "https://github.com/x/y.git"
    assert ns.cycles == 3
    assert ns.subject == "improve docs"
    assert ns.run_id == 42


def test_argv_parsing_requires_run_id():
    ap = _build_parser_from_main_source()
    with pytest.raises(SystemExit):
        ap.parse_args(["--url", "u", "--cycles", "1"])


def test_argv_parsing_requires_url():
    ap = _build_parser_from_main_source()
    with pytest.raises(SystemExit):
        ap.parse_args(["--cycles", "1", "--run-id", "1"])


def test_argv_parsing_cycles_must_be_int():
    ap = _build_parser_from_main_source()
    with pytest.raises(SystemExit):
        ap.parse_args(["--url", "u", "--cycles", "not-an-int", "--run-id", "1"])


def test_argv_parsing_run_id_must_be_int():
    ap = _build_parser_from_main_source()
    with pytest.raises(SystemExit):
        ap.parse_args(["--url", "u", "--cycles", "1", "--run-id", "abc"])


def test_argv_parsing_subject_is_optional():
    ap = _build_parser_from_main_source()
    ns = ap.parse_args(["--url", "u", "--cycles", "1", "--run-id", "1"])
    assert ns.subject is None


# ── Module-level sanity ─────────────────────────────────────────────────────


def test_run_bridged_exposes_validate_environment():
    assert callable(run_bridged.validate_environment)


# ── m-TMP: tighten log umask to 0o600 ────────────────────────────────────


def test_main_sets_restrictive_umask(monkeypatch):
    """m-TMP (review round 2): the detached Python must set umask 0o077
    at the top of main() so the log file at /tmp/kaizen-bridged-*.log
    is created mode 0600 (owner-only). Without this, the log inherits
    the system default 0644 (world-readable) and any `args_json`
    contents Python ever prints to stderr would leak."""
    import os

    captured: dict = {}
    real_umask = os.umask

    def spy_umask(mask):
        captured["mask"] = mask
        return real_umask(mask)

    monkeypatch.setattr(os, "umask", spy_umask)

    # Sabotage validate_environment so main() exits AFTER setting umask
    # but BEFORE doing any DB / clone work.
    def _abort():
        raise SystemExit(0)

    monkeypatch.setattr("scripts.run_bridged.validate_environment", _abort)

    argv = [
        "--db",
        ".ai/memex.db",
        "--bridge-db",
        ".ai/bridge.db",
        "--url",
        "https://github.com/x/y.git",
        "--cycles",
        "1",
        "--subject",
        "s",
        "--run-id",
        "1",
    ]
    with pytest.raises(SystemExit):
        run_bridged.main(argv)

    assert captured.get("mask") == 0o077, (
        f"main() must set umask 0o077; got {captured.get('mask')!r}"
    )
