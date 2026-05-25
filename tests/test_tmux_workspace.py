"""Tests for scripts/_tmux_workspace.py — post-spawn tmux layout/title hooks."""

from __future__ import annotations

import subprocess as real_subprocess

from scripts import _tmux_workspace


def _mk_proc(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return real_subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


# ── apply_main_vertical_layout ────────────────────────────────────────────


def test_apply_main_vertical_layout_runs_list_panes_and_select_layout(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if "list-panes" in argv:
            return _mk_proc(0, "%1 backend-engineer-1\n%2 software-architect-1\n")
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    _tmux_workspace.apply_main_vertical_layout(
        workspace_name="kaizen-cycle-1-1", main_agent="software-architect-1"
    )
    # Assert list-panes was called first, then select-layout, then swap-pane.
    cmds = [c[0:3] for c in calls]
    assert ["tmux", "list-panes", "-t"] in cmds
    assert ["tmux", "select-layout", "-t"] in cmds
    assert ["tmux", "swap-pane", "-t"] in cmds


def test_apply_main_vertical_layout_tolerates_no_server_running(monkeypatch):
    """tmux returning "no server running" must NOT raise; the helper exits early."""

    def fake_run(argv, **kwargs):
        return _mk_proc(1, "", "no server running on /tmp/tmux-1000/default")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    # Should NOT raise.
    _tmux_workspace.apply_main_vertical_layout(workspace_name="missing", main_agent="x")


def test_apply_main_vertical_layout_skips_swap_when_main_agent_absent(monkeypatch):
    """If main_agent doesn't match any pane title, no swap-pane call fires."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if "list-panes" in argv:
            return _mk_proc(0, "%1 backend-engineer-1\n%2 sdet-1\n")
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    _tmux_workspace.apply_main_vertical_layout(
        workspace_name="w", main_agent="software-architect-1"
    )
    swap_calls = [c for c in calls if "swap-pane" in c]
    assert swap_calls == []


def test_apply_main_vertical_layout_skips_swap_when_target_already_main(monkeypatch):
    """If the main_agent IS the first-listed pane (already main), no swap fires."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if "list-panes" in argv:
            return _mk_proc(0, "%1 software-architect-1\n%2 backend-engineer-1\n")
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    _tmux_workspace.apply_main_vertical_layout(
        workspace_name="w", main_agent="software-architect-1"
    )
    swap_calls = [c for c in calls if "swap-pane" in c]
    assert swap_calls == []


# ── set_pane_title ────────────────────────────────────────────────────────


def test_set_pane_title_renames_matching_pane(monkeypatch):
    """Pane title is updated to ``[w{wave_n}] {agent}`` for the matching pane."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if "list-panes" in argv:
            return _mk_proc(0, "%1 backend-engineer-1\n%2 sdet-1\n")
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    _tmux_workspace.set_pane_title(workspace_name="w", agent_name="backend-engineer-1", wave_n=2)
    select_calls = [c for c in calls if "select-pane" in c]
    assert len(select_calls) == 1
    call = select_calls[0]
    # The new title is the value after `-T`.
    assert "-T" in call
    assert call[call.index("-T") + 1] == "[w2] backend-engineer-1"
    # The target pane is the one whose title matched.
    assert "%1" in call


def test_set_pane_title_noop_when_no_pane_matches(monkeypatch):
    """No pane whose title contains agent_name → no select-pane call."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if "list-panes" in argv:
            return _mk_proc(0, "%1 sdet-1\n")
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    _tmux_workspace.set_pane_title(workspace_name="w", agent_name="backend-engineer-1", wave_n=1)
    assert [c for c in calls if "select-pane" in c] == []


def test_set_pane_title_tolerates_no_server_running(monkeypatch):
    """tmux returning "no server running" must NOT raise."""

    def fake_run(argv, **kwargs):
        return _mk_proc(1, "", "no server running on /tmp/tmux-1000/default")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    # Should NOT raise.
    _tmux_workspace.set_pane_title(workspace_name="w", agent_name="x", wave_n=1)
