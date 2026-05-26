"""Tests for scripts/_tmux_workspace.py — workspace layout + per-pane titles.

Covers the fix for kaizen#55: pane titles were stuck at ``general-purpose``
because the prior substring matcher could never identify which pane
belonged to which teammate, and the layout was a single-column
``main-vertical`` instead of the desired "main left + 2-column right
grid." The new API maps panes to agents positionally and folds the
right column via ``join-pane``.
"""

from __future__ import annotations

import subprocess as real_subprocess

from scripts import _tmux_workspace


def _mk_proc(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return real_subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


# ── apply_workspace_layout ────────────────────────────────────────────────


def test_apply_workspace_layout_returns_positional_pane_to_agent_map(monkeypatch):
    """pane_ids[i] zips with ordered_agents[i] regardless of pane titles."""

    def fake_run(argv, **kwargs):
        if "list-panes" in argv:
            # Note: titles are all ``general-purpose`` — the legacy substring
            # matcher would have found nothing. Positional zip still works.
            return _mk_proc(0, "%1\n%2\n%3\n%4\n")
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    result = _tmux_workspace.apply_workspace_layout(
        workspace_name="w",
        ordered_agents=[
            "software-architect-1",
            "backend-engineer-1",
            "sdet-1",
            "security-engineer-1",
        ],
    )
    assert result == {
        "%1": "software-architect-1",
        "%2": "backend-engineer-1",
        "%3": "sdet-1",
        "%4": "security-engineer-1",
    }


def test_apply_workspace_layout_runs_select_layout_and_join_panes(monkeypatch):
    """main-vertical fires, then join-pane pairs the right column (1+2, 3+4)."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if "list-panes" in argv:
            return _mk_proc(0, "%1\n%2\n%3\n%4\n%5\n")
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    _tmux_workspace.apply_workspace_layout(
        workspace_name="w",
        ordered_agents=["a", "b", "c", "d", "e"],
    )
    # select-layout main-vertical fires once.
    layout_calls = [c for c in calls if "select-layout" in c]
    assert len(layout_calls) == 1
    assert "main-vertical" in layout_calls[0]
    # join-pane fires for (2→1, 4→3) — only pairs in the right column.
    join_calls = [c for c in calls if "join-pane" in c]
    assert len(join_calls) == 2
    # First pair: source %3 joined into target %2
    assert "-s" in join_calls[0] and join_calls[0][join_calls[0].index("-s") + 1] == "%3"
    assert "-t" in join_calls[0] and join_calls[0][join_calls[0].index("-t") + 1] == "%2"
    # Second pair: source %5 joined into target %4
    assert join_calls[1][join_calls[1].index("-s") + 1] == "%5"
    assert join_calls[1][join_calls[1].index("-t") + 1] == "%4"


def test_apply_workspace_layout_swaps_main_agent_to_position_zero(monkeypatch):
    """If main_agent isn't pane_ids[0], swap-pane fires to promote it."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if "list-panes" in argv:
            return _mk_proc(0, "%1\n%2\n%3\n")
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    _tmux_workspace.apply_workspace_layout(
        workspace_name="w",
        ordered_agents=["backend-engineer-1", "software-architect-1", "sdet-1"],
        main_agent="software-architect-1",
    )
    swap_calls = [c for c in calls if "swap-pane" in c]
    assert len(swap_calls) == 1
    # Source = the architect's pane (%2), target = current main slot (%1)
    assert "-s" in swap_calls[0] and swap_calls[0][swap_calls[0].index("-s") + 1] == "%2"
    assert "-t" in swap_calls[0] and swap_calls[0][swap_calls[0].index("-t") + 1] == "%1"


def test_apply_workspace_layout_no_swap_when_main_agent_already_first(monkeypatch):
    """main_agent at index 0 → no swap-pane call."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if "list-panes" in argv:
            return _mk_proc(0, "%1\n%2\n")
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    _tmux_workspace.apply_workspace_layout(
        workspace_name="w",
        ordered_agents=["software-architect-1", "backend-engineer-1"],
        main_agent="software-architect-1",
    )
    assert [c for c in calls if "swap-pane" in c] == []


def test_apply_workspace_layout_no_swap_when_main_agent_absent(monkeypatch):
    """main_agent not in ordered_agents → no swap-pane call."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if "list-panes" in argv:
            return _mk_proc(0, "%1\n%2\n")
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    _tmux_workspace.apply_workspace_layout(
        workspace_name="w",
        ordered_agents=["backend-engineer-1", "sdet-1"],
        main_agent="software-architect-1",
    )
    assert [c for c in calls if "swap-pane" in c] == []


def test_apply_workspace_layout_tolerates_no_server_running(monkeypatch):
    """tmux returning "no server running" must NOT raise; helper returns {}."""

    def fake_run(argv, **kwargs):
        return _mk_proc(1, "", "no server running on /tmp/tmux-1000/default")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    result = _tmux_workspace.apply_workspace_layout(
        workspace_name="missing", ordered_agents=["a", "b"], main_agent="a"
    )
    assert result == {}


def test_apply_workspace_layout_no_join_with_only_one_right_pane(monkeypatch):
    """2 panes total → 1 main + 1 right pane → no join-pane needed."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if "list-panes" in argv:
            return _mk_proc(0, "%1\n%2\n")
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    _tmux_workspace.apply_workspace_layout(workspace_name="w", ordered_agents=["a", "b"])
    assert [c for c in calls if "join-pane" in c] == []


def test_apply_workspace_layout_skips_odd_right_pane(monkeypatch):
    """Odd-count right panes: pair (1,2)(3,4)…; the last leftover is left alone."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if "list-panes" in argv:
            return _mk_proc(0, "%1\n%2\n%3\n%4\n")
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    _tmux_workspace.apply_workspace_layout(workspace_name="w", ordered_agents=["a", "b", "c", "d"])
    # 3 right panes (%2, %3, %4) → only one pair (%3 joined into %2);
    # %4 stays on its own row.
    join_calls = [c for c in calls if "join-pane" in c]
    assert len(join_calls) == 1


def test_apply_workspace_layout_empty_agents_returns_empty(monkeypatch):
    """ordered_agents=[] short-circuits without any tmux call."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    result = _tmux_workspace.apply_workspace_layout(workspace_name="w", ordered_agents=[])
    assert result == {}
    assert calls == []


# ── set_pane_titles ────────────────────────────────────────────────────────


def test_set_pane_titles_targets_each_pane_by_id(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    _tmux_workspace.set_pane_titles(
        "w",
        {
            "%1": "[w1] backend-engineer-1",
            "%2": "[w1] sdet-1",
        },
    )
    select_calls = [c for c in calls if "select-pane" in c]
    assert len(select_calls) == 2
    # Each call targets a specific pane by %id and sets the title via -T.
    pane_to_title = {}
    for c in select_calls:
        pane_id = c[c.index("-t") + 1]
        title = c[c.index("-T") + 1]
        pane_to_title[pane_id] = title
    assert pane_to_title == {
        "%1": "[w1] backend-engineer-1",
        "%2": "[w1] sdet-1",
    }


def test_set_pane_titles_empty_dict_is_noop(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    _tmux_workspace.set_pane_titles("w", {})
    assert calls == []


def test_set_pane_titles_tolerates_no_server_running(monkeypatch):
    def fake_run(argv, **kwargs):
        return _mk_proc(1, "", "no server running on /tmp/tmux-1000/default")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    # Must NOT raise.
    _tmux_workspace.set_pane_titles("w", {"%1": "[w1] a", "%2": "[w1] b"})


def test_set_pane_titles_keeps_going_on_per_pane_hard_error(monkeypatch):
    """A single pane failing should not abort the rest (best-effort)."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        # Fail on %1; succeed on %2.
        if "-t" in argv and argv[argv.index("-t") + 1] == "%1":
            return _mk_proc(1, "", "can't find pane: %1")
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    _tmux_workspace.set_pane_titles("w", {"%1": "[w1] a", "%2": "[w1] b"})
    # Both panes were attempted (didn't bail on the first failure).
    select_calls = [c for c in calls if "select-pane" in c]
    assert len(select_calls) == 2
