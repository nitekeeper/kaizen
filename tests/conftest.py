"""Shared pytest fixtures for kaizen tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import loom_comms
from scripts.dispatch_templates import _reset_template_cache


# F16 hermeticity: a live Loom server on the dev host must never leak
# into the suite. Every test runs with the explicit kill-switch set and
# a cold detect cache; tests that exercise loom behaviour override the
# env (monkeypatch.setenv / delenv) and call `loom_comms.reset_cache()`
# themselves. The guarded live-roundtrip test (KAIZEN_LOOM_LIVE=1)
# deletes the kill-switch inside the test body.
@pytest.fixture(autouse=True)
def _isolate_loom_comms(monkeypatch):
    monkeypatch.setenv("KAIZEN_LOOM_COMMS", "0")
    loom_comms.reset_cache()
    yield
    loom_comms.reset_cache()


# Any test that mutates files in `internal/cycle/templates/` (or that
# depends on a freshly-read template body — e.g. byte-identity goldens,
# positional-clause asserts, frontmatter-parity tests) RELIES on this
# fixture to clear the module-level `_TEMPLATE_CACHE` in
# `scripts.dispatch_templates`. Autouse + function-scoped means every
# test sees a cold cache, so cross-test ordering can never cause one
# test's edits to bleed into another's rendered output.
@pytest.fixture(autouse=True)
def _isolate_template_cache():
    """Clear `scripts.dispatch_templates._TEMPLATE_CACHE` before each test.

    The cache is process-wide and otherwise persists across tests; a
    test that monkey-patches or rewrites a template file would
    otherwise still read the previous test's cached body. Clearing on
    setup (not teardown) guarantees the test's *own* renders are fresh
    without depending on prior teardowns having run.
    """
    _reset_template_cache()
    yield


# run-76 AI-4 rider (reviewer-imposed) — suite-wide isolation from the REAL
# tmux server's GLOBAL hook state (the kaizen#98 operator-config-drift class).
# `scripts.team_executor` installs/removes the `after-split-window[88]`
# reconcile hook at workspace boot / cycle teardown; LEGACY executor-driving
# test files (tests/test_team_executor_cleanup.py, tests/test_caveman_codec.py,
# tests/test_end_to_end_team_mode.py, tests/test_f3_fire_order.py, ...) drive
# `team_cycle_executor` without patching those seams. When pytest runs inside
# a MULTI-PANE tmux window, the real `apply_workspace_layout` returns a
# non-empty pane map, the install gate passes, and every such test would write
# a real `set-hook -g after-split-window[88]` + window tags on the developer's
# live server — a killed run could leak an active global hook. Stub the three
# seams to inert fakes for EVERY test. This does not blunt the dedicated
# coverage: tests/test_team_executor.py re-patches the same attributes with
# per-test recorders (a monkeypatch applied later in the fixture chain — its
# module autouse fixture and test-body patches — wins over this one), and the
# hook/fold tests in tests/test_tmux_config.py / tests/test_tmux_workspace.py /
# tests/test_tmux_hook_reconcile.py exercise the helper MODULES directly, not
# team_executor's rebindings.
@pytest.fixture(autouse=True)
def _isolate_team_window_hook_seams_suite_wide(monkeypatch):
    import scripts.team_executor as _team_executor

    monkeypatch.setattr(_team_executor, "install_team_window_hook", lambda team_id, **kw: True)
    monkeypatch.setattr(_team_executor, "remove_team_window_hook", lambda **kw: True)
    # None = "tmux soft-fail" → the pane-signature delta trigger stays
    # disarmed (and adds zero subprocess reads) unless a test scripts it.
    monkeypatch.setattr(_team_executor, "_pane_signature", lambda workspace_name: None)
    yield


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        encoding="utf-8",
    )


def _init_bare_remote(path: Path, branch: str) -> None:
    """Initialise an empty bare repo at `path` with `branch` as the initial branch."""
    path.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "-b", branch, str(path)],
        check=True,
        capture_output=True,
    )


def _seed_remote_from_scratch(tmp_path: Path, remote: Path, branch: str) -> None:
    """Push one README-only commit onto `branch` of `remote` from a throwaway repo."""
    safe = branch.replace("/", "_")
    seed = tmp_path / f"seed_{safe}"
    seed.mkdir()
    _git(["init", "-b", branch], seed)
    _git(["config", "user.email", "test@test.com"], seed)
    _git(["config", "user.name", "Test User"], seed)
    _git(["remote", "add", "origin", str(remote)], seed)
    (seed / "README.md").write_text(f"# kaizen test target ({branch})\n")
    _git(["add", "."], seed)
    _git(["commit", "-m", "initial"], seed)
    _git(["push", "-u", "origin", branch], seed)


@pytest.fixture
def bare_remote(tmp_path):
    """Empty bare repo acting as origin (initial branch `main`).

    Note: this fixture creates an *empty* remote; pair with `source_repo` to
    push commits before any clone.
    """
    remote = tmp_path / "remote.git"
    _init_bare_remote(remote, "main")
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
    _init_bare_remote(remote, "trunk")
    _seed_remote_from_scratch(tmp_path, remote, "trunk")
    return remote
