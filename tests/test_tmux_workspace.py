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


# ── kaizen#61 regression — workspace_name MUST NOT leak into argv ─────────
#
# CC team-mode panes belong to the orchestrator's current tmux window,
# NOT a session named after the team. The old code passed
# ``team_name = f"kaizen-cycle-{run_id}-{cycle_n}"`` as ``-t`` to
# list-panes / select-layout, which always hit "session not found" and
# the no-server soft-fail path — so the layout was never applied. The
# fix is to drop ``-t workspace_name`` from those two calls; the global
# pane-id targeting on swap-pane / join-pane / select-pane is correct
# and is retained.
#
# These tests are deliberately ARGV-shape assertions, NOT canned-mock
# returns. The original bug shipped because the mock accepted any argv
# unconditionally (mocks-must-match-reality, per the memory note).


def test_list_panes_argv_does_not_contain_workspace_name(monkeypatch):
    """Regression: ``-t <team_name>`` must NOT appear on list-panes (kaizen#61)."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if "list-panes" in argv:
            return _mk_proc(0, "%1\n%2\n")
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    _tmux_workspace.apply_workspace_layout(
        workspace_name="bogus-team-name-that-is-not-a-tmux-target",
        ordered_agents=["a", "b"],
    )
    list_panes_calls = [c for c in calls if "list-panes" in c]
    assert list_panes_calls, "list-panes was never called"
    for c in list_panes_calls:
        assert "bogus-team-name-that-is-not-a-tmux-target" not in c, (
            f"list-panes argv leaked workspace_name (kaizen#61 regression): {c}"
        )
        # And the explicit shape: no ``-t`` flag at all on list-panes.
        assert "-t" not in c, f"list-panes argv unexpectedly carries -t (kaizen#61): {c}"


def test_select_layout_argv_does_not_contain_workspace_name(monkeypatch):
    """Regression: ``-t <team_name>`` must NOT appear on select-layout (kaizen#61)."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if "list-panes" in argv:
            return _mk_proc(0, "%1\n%2\n%3\n%4\n")
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    _tmux_workspace.apply_workspace_layout(
        workspace_name="bogus-team-name-that-is-not-a-tmux-target",
        ordered_agents=["a", "b", "c", "d"],
    )
    layout_calls = [c for c in calls if "select-layout" in c]
    assert layout_calls, "select-layout was never called"
    for c in layout_calls:
        assert "bogus-team-name-that-is-not-a-tmux-target" not in c, (
            f"select-layout argv leaked workspace_name (kaizen#61): {c}"
        )
        assert "-t" not in c, f"select-layout argv unexpectedly carries -t (kaizen#61): {c}"


def test_swap_pane_join_pane_select_pane_still_use_global_pane_id(monkeypatch):
    """Confirm the non-kaizen#61 calls keep their `-t %id` (pane-id is global)."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if "list-panes" in argv:
            return _mk_proc(0, "%1\n%2\n%3\n%4\n")
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    _tmux_workspace.apply_workspace_layout(
        workspace_name="w",
        ordered_agents=["a", "b", "c", "d"],
        main_agent="b",  # forces a swap-pane
    )
    swap_calls = [c for c in calls if "swap-pane" in c]
    join_calls = [c for c in calls if "join-pane" in c]
    assert swap_calls, "swap-pane was never called"
    assert join_calls, "join-pane was never called"
    for c in swap_calls + join_calls:
        # Each call MUST carry ``-t %N`` (global pane-id), not a workspace target.
        assert "-t" in c, f"global pane-id targeting was dropped: {c}"
        target = c[c.index("-t") + 1]
        assert target.startswith("%"), f"-t target should be a %N pane-id, got: {target!r}"


# ── _sanitize_title — strip → escape → left-truncate ─────────────────────


def test_sanitize_title_strips_control_chars():
    """C0 controls (incl. ESC \\x1b) and DEL are removed."""
    out = _tmux_workspace._sanitize_title("hello\x00world\x1b[31mred\x07\x7f")
    assert "\x00" not in out and "\x1b" not in out and "\x07" not in out and "\x7f" not in out
    # ESC[31m → "[31m" (the ESC byte removed; the remainder is now inert text).
    assert out == "helloworld[31mred"


def test_sanitize_title_strips_unicode_bidi_controls():
    """Bidi controls U+202A-U+202E and U+2066-U+2069 are removed.

    Bidi codepoints are constructed via ``chr()`` so the literal
    Trojan-Source bytes don't appear anywhere in this source file
    (bandit B613).
    """
    lre = chr(0x202A)  # LEFT-TO-RIGHT EMBEDDING
    rlo = chr(0x202E)  # RIGHT-TO-LEFT OVERRIDE
    fsi = chr(0x2068)  # FIRST STRONG ISOLATE
    pdi = chr(0x2069)  # POP DIRECTIONAL ISOLATE
    raw = f"back{lre}end-engineer{fsi}-1{pdi}{rlo}"
    out = _tmux_workspace._sanitize_title(raw)
    assert lre not in out
    assert rlo not in out
    assert fsi not in out
    assert pdi not in out
    assert out == "backend-engineer-1"


def test_sanitize_title_escapes_hash_to_double_hash():
    """Single # introduces a tmux format spec; doubling produces a literal."""
    assert _tmux_workspace._sanitize_title("#H-host") == "##H-host"
    assert _tmux_workspace._sanitize_title("a#{b}c") == "a##{b}c"


def test_sanitize_title_returns_question_mark_on_empty():
    """Empty / None / all-stripped inputs collapse to '?'."""
    assert _tmux_workspace._sanitize_title("") == "?"
    assert _tmux_workspace._sanitize_title(None) == "?"  # type: ignore[arg-type]
    assert _tmux_workspace._sanitize_title("\x00\x01\x1b\x7f") == "?"


def test_sanitize_title_left_truncates_with_ellipsis():
    """Long titles keep the meaningful suffix; an ellipsis marks the cut."""
    long = "a" * 100 + "role-id-1"
    out = _tmux_workspace._sanitize_title(long, max_len=20)
    assert len(out) == 20
    assert out.endswith("role-id-1")
    assert out.startswith("…")


def test_sanitize_title_preserves_wave_prefix_under_truncation():
    """[wN] prefix survives; truncation eats from the middle."""
    raw = "[w3] " + ("x" * 200) + "backend-engineer-1"
    out = _tmux_workspace._sanitize_title(raw, max_len=30)
    assert out.startswith("[w3] ")
    assert out.endswith("backend-engineer-1")
    assert "…" in out
    assert len(out) == 30


def test_sanitize_title_short_input_passthrough():
    """Already-clean, already-short titles are returned unchanged."""
    assert _tmux_workspace._sanitize_title("backend-engineer-1") == "backend-engineer-1"
    assert _tmux_workspace._sanitize_title("[w1] arch-1") == "[w1] arch-1"


def test_sanitize_title_applies_in_strict_order(monkeypatch):
    """End-to-end: a title with control + hash + length all get fixed correctly."""
    rlo = chr(0x202E)  # RIGHT-TO-LEFT OVERRIDE
    raw = f"\x1b[w2] arch#1{rlo}"
    out = _tmux_workspace._sanitize_title(raw, max_len=64)
    # ESC + bidi stripped, # escaped to ##, no truncation needed.
    assert out == "[w2] arch##1"


# ── set_pane_title (singular) ─────────────────────────────────────────────


def test_set_pane_title_calls_select_pane_with_sanitized_title(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    _tmux_workspace.set_pane_title("%5", "back#end-engineer-1\x1bhello")
    assert len(calls) == 1
    argv = calls[0]
    # argv[0] is the "tmux" binary; argv[1] is the subcommand.
    assert argv[0] == "tmux"
    assert "select-pane" in argv
    assert argv[argv.index("-t") + 1] == "%5"
    # Title is sanitized: # → ##, ESC stripped.
    assert argv[argv.index("-T") + 1] == "back##end-engineer-1hello"


def test_set_pane_title_tolerates_no_server(monkeypatch):
    def fake_run(argv, **kwargs):
        return _mk_proc(1, "", "no server running on /tmp/tmux-1000/default")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    # Must NOT raise.
    _tmux_workspace.set_pane_title("%1", "anything")


def test_set_pane_titles_uses_sanitizer(monkeypatch):
    """Bulk titler must also sanitize (regression — sanitizer was applied only in singular)."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    _tmux_workspace.set_pane_titles(
        "w",
        {
            "%1": "[w1] back#end-engineer-1",
            "%2": "[w1] s\x1bdet-1",
        },
    )
    titles = {c[c.index("-t") + 1]: c[c.index("-T") + 1] for c in calls if "select-pane" in c}
    assert titles == {
        "%1": "[w1] back##end-engineer-1",
        "%2": "[w1] sdet-1",
    }


# ── fold_right_column ─────────────────────────────────────────────────────


def test_fold_right_column_pairs_consecutive_right_panes(monkeypatch):
    """Pair (1+2), (3+4) etc.; main pane at index 0 is untouched."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    _tmux_workspace.fold_right_column(["%1", "%2", "%3", "%4", "%5"])
    join_calls = [c for c in calls if "join-pane" in c]
    assert len(join_calls) == 2
    # First pair: %3 → %2; second pair: %5 → %4.
    assert join_calls[0][join_calls[0].index("-s") + 1] == "%3"
    assert join_calls[0][join_calls[0].index("-t") + 1] == "%2"
    assert join_calls[1][join_calls[1].index("-s") + 1] == "%5"
    assert join_calls[1][join_calls[1].index("-t") + 1] == "%4"


def test_fold_right_column_noop_with_lt_two_right_panes(monkeypatch):
    """0 or 1 right panes → no join-pane call."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    _tmux_workspace.fold_right_column(["%1"])
    _tmux_workspace.fold_right_column(["%1", "%2"])
    assert [c for c in calls if "join-pane" in c] == []
