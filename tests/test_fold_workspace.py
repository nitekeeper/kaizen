"""Tests for the orchestrator-side workspace fold (kaizen#86).

`scripts._tmux_workspace.fold_current_window` + the `scripts.fold_workspace`
CLI. The fold is what the orchestrator runs (serviced from the `apply_layout`
bridge request) so the grid is applied in the window that actually holds the
teammate panes — unlike the in-process fold, which no-ops from the detached
run_bridged process.

Mocks ONLY the `subprocess.run` boundary (mirrors tests/test_tmux_workspace.py);
the fold logic + pane-list parsing + #81 PM-prepend are exercised for real.
"""

from __future__ import annotations

import types

import pytest

import scripts._tmux_workspace as tw
from scripts import fold_workspace


def _mk_proc(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture(autouse=True)
def _reset_layout_warn(monkeypatch):
    # _resolve_layout warns-once per process per bad value; reset so tests don't leak.
    monkeypatch.setattr(tw, "_warned_layout_values", set())


def test_fold_current_window_grids_all_teammates_orchestrator_excluded(monkeypatch):
    """TMUX_PANE set (orchestrator pane %1 excluded from the list): select-layout
    main-vertical fires, then join-pane pairs ALL 4 teammates (%3->%2, %5->%4) —
    the PM pane is prepended (kaizen#81) so the first teammate is not dropped."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if "list-panes" in argv:
            return _mk_proc(0, "%1\n%2\n%3\n%4\n%5\n")  # %1 = orchestrator
        return _mk_proc(0, "")

    monkeypatch.setattr(tw.subprocess, "run", fake_run)
    monkeypatch.setenv("TMUX_PANE", "%1")
    monkeypatch.setenv("KAIZEN_TEAMMATE_LAYOUT", "grid-2col")

    tw.fold_current_window()

    layout_calls = [c for c in calls if "select-layout" in c]
    assert len(layout_calls) == 1 and "main-vertical" in layout_calls[0]
    join_calls = [c for c in calls if "join-pane" in c]
    assert len(join_calls) == 2, f"expected all 4 teammates folded, got {join_calls}"
    assert join_calls[0][join_calls[0].index("-s") + 1] == "%3"
    assert join_calls[0][join_calls[0].index("-t") + 1] == "%2"
    assert join_calls[1][join_calls[1].index("-s") + 1] == "%5"
    assert join_calls[1][join_calls[1].index("-t") + 1] == "%4"
    for c in join_calls:
        assert "%1" not in c, f"PM pane must not be folded: {c}"


def test_fold_current_window_stripes_skips_fold(monkeypatch):
    """KAIZEN_TEAMMATE_LAYOUT=stripes → even-vertical, no join-pane."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if "list-panes" in argv:
            return _mk_proc(0, "%1\n%2\n%3\n")
        return _mk_proc(0, "")

    monkeypatch.setattr(tw.subprocess, "run", fake_run)
    monkeypatch.setenv("TMUX_PANE", "%1")
    monkeypatch.setenv("KAIZEN_TEAMMATE_LAYOUT", "stripes")

    tw.fold_current_window()
    assert any("even-vertical" in c for c in calls if "select-layout" in c)
    assert [c for c in calls if "join-pane" in c] == []


def test_fold_current_window_tolerates_no_server(monkeypatch):
    """No tmux server → no raise, no fold. `_tmux_unavailable` keys off a
    non-zero returncode whose stderr mentions 'no server running', so the
    list-panes probe returns that proc and the fold bails cleanly."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if "list-panes" in argv:
            return _mk_proc(1, stderr="no server running on /tmp/tmux-1000/default")
        return _mk_proc(0, "")

    monkeypatch.setattr(tw.subprocess, "run", fake_run)
    # Must not raise, and must not attempt any layout/join once the server is gone.
    tw.fold_current_window()
    assert [c for c in calls if "select-layout" in c or "join-pane" in c] == []


def test_fold_current_window_no_panes_is_noop(monkeypatch):
    def fake_run(argv, **kwargs):
        if "list-panes" in argv:
            return _mk_proc(0, "")  # no panes
        return _mk_proc(0, "")

    monkeypatch.setattr(tw.subprocess, "run", fake_run)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    tw.fold_current_window()  # no raise


def test_fold_workspace_cli_returns_zero(monkeypatch):
    """The CLI always exits 0 (best-effort) and forwards --team-id as workspace_name."""
    seen = {}

    def fake_fold(*, workspace_name=""):
        seen["workspace_name"] = workspace_name

    monkeypatch.setattr(fold_workspace, "fold_current_window", fake_fold)
    assert fold_workspace.main(["--team-id", "kaizen-cycle-50-1"]) == 0
    assert seen["workspace_name"] == "kaizen-cycle-50-1"


def test_fold_workspace_cli_swallows_errors(monkeypatch):
    def boom(*, workspace_name=""):
        raise RuntimeError("tmux exploded")

    monkeypatch.setattr(fold_workspace, "fold_current_window", boom)
    # Best-effort: a fold failure must never fail the orchestrator's write-back.
    assert fold_workspace.main([]) == 0
