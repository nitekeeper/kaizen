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

import pytest

from scripts import _tmux_workspace


@pytest.fixture(autouse=True)
def _isolate_tmux_pane(monkeypatch):
    """Ensure TMUX_PANE is unset for every test in this module by default.

    Otherwise a developer running pytest from inside a real tmux session
    sees the outer-pane id collide with mocked ids (e.g. %1). Concretely:
    if ``TMUX_PANE=%1`` is inherited from the outer terminal,
    ``_list_pane_ids`` would drop ``%1`` from the mocked list-panes
    output, the positional zip against ``ordered_agents`` would silently
    drop a real teammate, and tests that compare against a fixed
    pane→agent map would fail non-deterministically.

    Tests that need TMUX_PANE set (the kaizen#66 orchestrator-exclusion
    tests) re-set it via ``monkeypatch.setenv("TMUX_PANE", ...)`` — that
    overrides this autouse delenv. See kaizen#72 item 1.
    """
    monkeypatch.delenv("TMUX_PANE", raising=False)


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
    """If main_agent isn't pane_ids[0], swap-pane fires to promote it.

    NB: this exercises the non-tmux / no-TMUX_PANE fallback path (kaizen#66).
    With TMUX_PANE set the orchestrator is excluded at source and no swap
    is needed; that path is covered separately.
    """
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if "list-panes" in argv:
            return _mk_proc(0, "%1\n%2\n%3\n")
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    monkeypatch.delenv("TMUX_PANE", raising=False)
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
    monkeypatch.delenv("TMUX_PANE", raising=False)
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
    # kaizen#64 — set_pane_title now fires TWO tmux calls per pane:
    #   1. set-option -p @desired_title (border-rendered, OSC-immune)
    #   2. select-pane -T (legacy pane_title for non-agent-teams configs)
    assert len(calls) == 2
    # First call is the @desired_title persist.
    set_opt_calls = [c for c in calls if "set-option" in c]
    assert len(set_opt_calls) == 1
    sa = set_opt_calls[0]
    assert sa[0] == "tmux"
    assert "@desired_title" in sa
    assert sa[sa.index("-t") + 1] == "%5"
    assert sa[sa.index("@desired_title") + 1] == "back##end-engineer-1hello"
    # Second call is the legacy select-pane -T.
    sp_calls = [c for c in calls if "select-pane" in c]
    assert len(sp_calls) == 1
    argv = sp_calls[0]
    assert argv[0] == "tmux"
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


# ── kaizen#66 — orchestrator pane identity via TMUX_PANE ──────────────────
#
# Before this fix ``_list_pane_ids`` returned the orchestrator's own pane
# alongside the teammate panes; the positional zip against ``ordered_agents``
# then re-classified the PM as a teammate. A reactive swap-pane block
# patched over it at the cost of a user-visible title-flicker race.
#
# The fix reads ``$TMUX_PANE`` at the source and excludes the orchestrator
# pane_id from the returned list. ``TMUX_PANE`` is stable for the life of
# the pane, so this is the authoritative "this is my own pane" signal.


def test_list_pane_ids_excludes_orchestrator_when_tmux_pane_set(monkeypatch):
    """kaizen#66: TMUX_PANE=orchestrator pid → orchestrator dropped from list."""

    def fake_run(argv, **kwargs):
        if "list-panes" in argv:
            return _mk_proc(0, "%1\n%2\n%3\n%4\n")
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    monkeypatch.setenv("TMUX_PANE", "%1")
    out = _tmux_workspace._list_pane_ids("w")
    assert out == ["%2", "%3", "%4"], (
        f"orchestrator pane %1 must be dropped from list-panes output, got {out}"
    )


def test_list_pane_ids_full_list_when_tmux_pane_unset(monkeypatch):
    """No TMUX_PANE (CI / headless) → full list returned, swap-pane fallback applies."""

    def fake_run(argv, **kwargs):
        if "list-panes" in argv:
            return _mk_proc(0, "%1\n%2\n%3\n")
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    out = _tmux_workspace._list_pane_ids("w")
    assert out == ["%1", "%2", "%3"]


def test_list_pane_ids_full_list_when_tmux_pane_not_in_list(monkeypatch):
    """TMUX_PANE set but pane not among list-panes output → list returned unchanged."""

    def fake_run(argv, **kwargs):
        if "list-panes" in argv:
            return _mk_proc(0, "%5\n%6\n")
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    monkeypatch.setenv("TMUX_PANE", "%99")
    out = _tmux_workspace._list_pane_ids("w")
    assert out == ["%5", "%6"]


def test_apply_workspace_layout_no_swap_pane_when_orchestrator_excluded(monkeypatch):
    """kaizen#66: with TMUX_PANE excluding the orchestrator, no reactive swap-pane fires.

    Three-snapshot invariant: the zip is correct by construction (positional),
    so the swap-pane block must NOT fire when the orchestrator id is known.
    """
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if "list-panes" in argv:
            # %1 is the orchestrator; %2/%3/%4 are teammates.
            return _mk_proc(0, "%1\n%2\n%3\n%4\n")
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    monkeypatch.setenv("TMUX_PANE", "%1")
    result = _tmux_workspace.apply_workspace_layout(
        workspace_name="w",
        ordered_agents=["backend-engineer-1", "software-architect-1", "sdet-1"],
        main_agent="software-architect-1",
    )
    # %1 (orchestrator) MUST NOT appear in the pane→agent map.
    assert "%1" not in result, f"orchestrator pane %1 leaked into map: {result}"
    # Teammates are zipped positionally against the post-exclusion list.
    assert result == {
        "%2": "backend-engineer-1",
        "%3": "software-architect-1",
        "%4": "sdet-1",
    }
    # No swap-pane fired: orchestrator-aware exclusion makes it unnecessary.
    swap_calls = [c for c in calls if "swap-pane" in c]
    assert swap_calls == [], (
        f"reactive swap-pane block fired despite TMUX_PANE being set "
        f"(kaizen#66 regression): {swap_calls}"
    )


def test_apply_workspace_layout_swap_pane_fallback_when_no_tmux_pane(monkeypatch):
    """kaizen#66: the swap-pane fallback still works when TMUX_PANE is unset."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if "list-panes" in argv:
            return _mk_proc(0, "%1\n%2\n%3\n")
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    _tmux_workspace.apply_workspace_layout(
        workspace_name="w",
        ordered_agents=["backend-engineer-1", "software-architect-1", "sdet-1"],
        main_agent="software-architect-1",
    )
    swap_calls = [c for c in calls if "swap-pane" in c]
    # Source = the architect's pane (%2), target = current main slot (%1).
    assert len(swap_calls) == 1, f"expected 1 swap-pane in fallback path, got: {swap_calls}"
    assert swap_calls[0][swap_calls[0].index("-s") + 1] == "%2"
    assert swap_calls[0][swap_calls[0].index("-t") + 1] == "%1"


def test_apply_workspace_layout_pins_orchestrator_title_at_boot(monkeypatch):
    """kaizen#66: the orchestrator pane gets the reserved PM title at workspace setup."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if "list-panes" in argv:
            return _mk_proc(0, "%1\n%2\n%3\n")
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    monkeypatch.setenv("TMUX_PANE", "%1")
    monkeypatch.delenv("KAIZEN_PM_PANE_GLYPH", raising=False)
    _tmux_workspace.apply_workspace_layout(
        workspace_name="w",
        ordered_agents=["arch-1", "be-1"],
    )
    # A select-pane -T call targeting the orchestrator with the reserved
    # title must have fired exactly once during setup.
    pm_calls = [
        c
        for c in calls
        if "select-pane" in c
        and "-t" in c
        and c[c.index("-t") + 1] == "%1"
        and "-T" in c
        and "team-lead / PM" in c[c.index("-T") + 1]
    ]
    assert len(pm_calls) >= 1, f"orchestrator title not pinned at boot; calls: {calls}"
    # Default glyph is U+25CF ●.
    assert "● team-lead / PM" in pm_calls[0][pm_calls[0].index("-T") + 1]


def test_pin_orchestrator_title_uses_kaizen_pm_pane_glyph_env(monkeypatch):
    """kaizen#66: ``KAIZEN_PM_PANE_GLYPH`` env override is honored."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    monkeypatch.setenv("KAIZEN_PM_PANE_GLYPH", "*")
    _tmux_workspace.pin_orchestrator_title("%7")
    titles = [c[c.index("-T") + 1] for c in calls if "select-pane" in c and "-T" in c]
    assert titles == ["* team-lead / PM"], f"expected ASCII glyph fallback, got: {titles}"


def test_pin_orchestrator_title_handles_empty_glyph(monkeypatch):
    """kaizen#66: empty glyph drops the leading separator — title is just ``team-lead / PM``."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    monkeypatch.setenv("KAIZEN_PM_PANE_GLYPH", "")
    _tmux_workspace.pin_orchestrator_title("%7")
    titles = [c[c.index("-T") + 1] for c in calls if "select-pane" in c and "-T" in c]
    assert titles == ["team-lead / PM"], f"empty glyph should drop separator, got: {titles}"


def test_three_snapshot_invariant_orchestrator_pane_stable(monkeypatch):
    """kaizen#66: across teammate-spawn + teammate-exit, orchestrator pane is unchanged.

    Simulates: initial list-panes returns [orchestrator, t1, t2]; a teammate-
    spawn event re-lists [orchestrator, t1, t2, t3]; a teammate-exit event
    re-lists [orchestrator, t1, t2]. Across all three snapshots, the
    orchestrator pane_id is dropped, never reassigned to a teammate role,
    and its title is never touched by the layout helper outside the boot
    pin call.
    """
    snapshots = [
        "%1\n%2\n%3\n",  # initial
        "%1\n%2\n%3\n%4\n",  # after teammate spawn
        "%1\n%2\n%3\n",  # after teammate exit
    ]
    snap_idx = [0]
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if "list-panes" in argv:
            out = snapshots[min(snap_idx[0], len(snapshots) - 1)]
            return _mk_proc(0, out)
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    monkeypatch.setenv("TMUX_PANE", "%1")

    # Three snapshots in sequence.
    results = []
    for i in range(3):
        snap_idx[0] = i
        out = _tmux_workspace._list_pane_ids("w")
        results.append(out)
    assert results == [
        ["%2", "%3"],
        ["%2", "%3", "%4"],
        ["%2", "%3"],
    ]
    # The orchestrator pane (%1) is never in any snapshot.
    for r in results:
        assert "%1" not in r


# ── pin_orchestrator_title — direct unit tests ────────────────────────────


def test_pin_orchestrator_title_default_glyph(monkeypatch):
    """Default glyph is U+25CF ●; title is ``● team-lead / PM``."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    monkeypatch.delenv("KAIZEN_PM_PANE_GLYPH", raising=False)
    _tmux_workspace.pin_orchestrator_title("%3")
    select_calls = [c for c in calls if "select-pane" in c]
    assert len(select_calls) == 1
    assert select_calls[0][select_calls[0].index("-T") + 1] == "● team-lead / PM"
    assert select_calls[0][select_calls[0].index("-t") + 1] == "%3"


def test_pin_orchestrator_title_explicit_glyph_arg(monkeypatch):
    """Explicit ``glyph=`` kwarg beats the env."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    monkeypatch.setenv("KAIZEN_PM_PANE_GLYPH", "*")  # would lose to explicit arg
    _tmux_workspace.pin_orchestrator_title("%3", glyph="+")
    titles = [c[c.index("-T") + 1] for c in calls if "select-pane" in c]
    assert titles == ["+ team-lead / PM"]


def test_orchestrator_pane_id_returns_none_when_unset(monkeypatch):
    monkeypatch.delenv("TMUX_PANE", raising=False)
    assert _tmux_workspace._orchestrator_pane_id() is None


def test_orchestrator_pane_id_returns_strip_value(monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "  %42  ")
    assert _tmux_workspace._orchestrator_pane_id() == "%42"


def test_orchestrator_pane_id_treats_empty_as_unset(monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "")
    assert _tmux_workspace._orchestrator_pane_id() is None


# ── kaizen#71 — pin fires even when _list_pane_ids returns empty ──────────
#
# Latent bug surfaced by sdet-1 during PR #70 review: the early-return
# ``if pane_ids is None or not pane_ids: return {}`` fires BEFORE the
# ``pin_orchestrator_title(lead_pane_id)`` call. If kaizen ever calls the
# layout helper before any teammates are spawned (dry-run, probe, or a
# future code path that wires layout earlier), the orchestrator title
# would not be pinned even when TMUX_PANE is set and tmux is up.
#
# Fix: pin is independent of teammate count — move it ABOVE the empty-
# list guard so it always fires when TMUX_PANE is set.


def test_apply_workspace_layout_pins_orchestrator_when_pane_ids_empty(monkeypatch):
    """kaizen#71: pin fires when list-panes returns empty but TMUX_PANE is set."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if "list-panes" in argv:
            # list-panes succeeds but returns no panes — simulates a
            # dry-run / probe before any teammate panes are present.
            return _mk_proc(0, "")
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    monkeypatch.setenv("TMUX_PANE", "%1")
    monkeypatch.delenv("KAIZEN_PM_PANE_GLYPH", raising=False)

    result = _tmux_workspace.apply_workspace_layout(
        workspace_name="w",
        ordered_agents=["arch-1", "be-1"],
    )
    assert result == {}  # no teammate panes → empty map (unchanged contract)

    # The PM pin MUST still have fired — target the orchestrator's pane
    # (%1) with the reserved title regardless of teammate count.
    pm_calls = [
        c
        for c in calls
        if "select-pane" in c
        and "-t" in c
        and c[c.index("-t") + 1] == "%1"
        and "-T" in c
        and "team-lead / PM" in c[c.index("-T") + 1]
    ]
    assert len(pm_calls) >= 1, (
        "kaizen#71: orchestrator title was NOT pinned when list-panes "
        f"returned empty (pin fires after the empty-list guard); calls: {calls}"
    )


# ── kaizen#72 item 2 — layout completes when pin_orchestrator_title raises
#
# pin_orchestrator_title is built on _persist_desired_title + set_pane_title,
# both fail-tolerant. But the sequence "pin fails first → continue to
# select-layout / join-pane / set_pane_titles" isn't directly exercised.
# This test pins that contract: a buggy pin must NOT abort the layout.


def test_apply_workspace_layout_continues_when_pin_orchestrator_title_raises(monkeypatch):
    """kaizen#72.2: a raise from pin_orchestrator_title must NOT abort layout."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if "list-panes" in argv:
            return _mk_proc(0, "%1\n%2\n%3\n")
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    monkeypatch.setenv("TMUX_PANE", "%1")

    def boom(*args, **kwargs):
        raise RuntimeError("simulated pin failure (kaizen#72.2)")

    monkeypatch.setattr(_tmux_workspace, "pin_orchestrator_title", boom)

    # Must NOT raise — layout must complete with the post-exclusion
    # positional map. If pin's exception escapes, the whole layout helper
    # aborts and pane→agent mapping is lost.
    result = _tmux_workspace.apply_workspace_layout(
        workspace_name="w",
        ordered_agents=["be-1", "sdet-1"],
    )
    # %1 (orchestrator) is excluded; %2/%3 zip with be-1/sdet-1.
    assert result == {"%2": "be-1", "%3": "sdet-1"}, (
        f"kaizen#72.2: layout did not complete after pin raised; got: {result}"
    )
    # select-layout main-vertical MUST have fired (proves we passed the
    # pin call site without aborting).
    layout_calls = [c for c in calls if "select-layout" in c]
    assert len(layout_calls) == 1, (
        "kaizen#72.2: select-layout did not fire after pin raised — "
        f"the exception escaped apply_workspace_layout. calls: {calls}"
    )


# ── kaizen#72 item 2 (helper-side) — pin_orchestrator_title's own
# "never raises" contract.
#
# apply_workspace_layout wraps pin_orchestrator_title in a try/except as
# belt-and-suspenders (see test_apply_workspace_layout_continues_when_
# pin_orchestrator_title_raises above), but the authoritative contract
# lives on pin_orchestrator_title itself — its docstring states "Tolerant
# of 'tmux server not running' / 'pane gone' — never raises." This test
# pins that contract directly at the source: if either internal helper
# (_persist_desired_title or set_pane_title) drops its fail-tolerance in
# the future, THIS test fails first — closer to the cause than the
# caller-side test, and survives even if a future refactor removes the
# caller-side try/except.
#
# software-architect-1 reviewer note (PR #70 follow-up): the regression
# for #72.2 should live on the helper, not just the caller.


def test_pin_orchestrator_title_never_raises_when_helpers_fail(monkeypatch):
    """kaizen#72.2 (helper-side): pin contract is "never raises".

    Exercises the two failure modes the internal helpers
    (``_persist_desired_title`` and ``set_pane_title``) are documented
    to absorb: tmux returning a hard error returncode (pane gone /
    other) and tmux being unavailable (no server). pin's docstring
    contract is "never raises"; this test pins that contract at the
    helper level so a future refactor that removes the helpers'
    returncode tolerance fires HERE first — closer to the source than
    the caller-side ``apply_workspace_layout`` test.
    """

    # ── Scenario 1: tmux subprocess returns hard error (pane gone) ──
    # Both ``_persist_desired_title`` (set-option) and ``set_pane_title``
    # (select-pane) hit subprocess errors. Helpers must swallow + emit
    # a stderr warning; pin must NOT raise.
    def fake_run_hard_error(argv, **kwargs):
        return _mk_proc(1, "", "can't find pane: %9")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run_hard_error)
    result = _tmux_workspace.pin_orchestrator_title("%9")
    # The helper returns None (no-return function). Pinning the current
    # return shape here means a future signature change that adds a
    # raise-on-error return becomes a test failure rather than silent
    # behavioral drift.
    assert result is None, (
        f"pin_orchestrator_title should return None on hard tmux error; got: {result!r}"
    )

    # ── Scenario 2: tmux server unavailable (soft "no server") ──────
    # The other path the helpers defend against — distinct from a
    # hard returncode error, routed through ``_tmux_unavailable``.
    def fake_run_no_server(argv, **kwargs):
        return _mk_proc(1, "", "no server running on /tmp/tmux-1000/default")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run_no_server)
    result2 = _tmux_workspace.pin_orchestrator_title("%9")
    assert result2 is None, (
        f"pin_orchestrator_title should return None when tmux server is missing; got: {result2!r}"
    )


# ── kaizen#72 item 1 — autouse fixture is load-bearing ────────────────────
#
# Proves the module-level autouse ``_isolate_tmux_pane`` fixture prevents
# a real failure: if TMUX_PANE leaks from the developer's outer terminal
# and happens to match a mocked pane id, the positional zip drops a real
# teammate. This test simulates that scenario (without the autouse
# fixture, it would fail).
#
# This test deliberately does NOT setenv TMUX_PANE — it relies on the
# autouse delenv. We assert the resulting pane→agent map contains every
# teammate (i.e., none was dropped by an outer-tmux %1 collision).


def test_autouse_tmux_pane_isolation_is_load_bearing(monkeypatch):
    """kaizen#72.1: the autouse delenv must keep TMUX_PANE unset.

    Without the fixture, a developer with ``TMUX_PANE=%1`` in their
    environment would see ``_list_pane_ids`` drop ``%1`` from the mocked
    output, the positional zip would collapse the agent list by one, and
    the assertion below would fail. The fixture's removal would
    reproduce that failure mode.
    """
    # Sanity: the autouse fixture is in effect — TMUX_PANE is unset.
    import os

    assert "TMUX_PANE" not in os.environ, (
        "autouse fixture did not unset TMUX_PANE — test pollution risk"
    )

    def fake_run(argv, **kwargs):
        if "list-panes" in argv:
            return _mk_proc(0, "%1\n%2\n%3\n")
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    result = _tmux_workspace.apply_workspace_layout(
        workspace_name="w",
        ordered_agents=["arch-1", "be-1", "sdet-1"],
    )
    # All three teammates present — %1 was NOT dropped as a fake-
    # orchestrator. If a stale TMUX_PANE=%1 had leaked, ``arch-1`` would
    # be missing from this map.
    assert result == {
        "%1": "arch-1",
        "%2": "be-1",
        "%3": "sdet-1",
    }, f"outer-tmux TMUX_PANE may have leaked into the test; got: {result}"


# ── kaizen#68 iter 3 — @kaizen_team_id pane tagging ───────────────────────


def test_tag_pane_team_id_sets_per_pane_user_option(monkeypatch):
    """``tag_pane_team_id`` writes the team_id under
    ``@kaizen_team_id`` via ``tmux set-option -p -t <pane>``.
    """
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    _tmux_workspace.tag_pane_team_id("%5", "team-aaa-aaa")
    assert calls == [
        [
            "tmux",
            "set-option",
            "-p",
            "-t",
            "%5",
            _tmux_workspace.KAIZEN_TEAM_ID_OPTION,
            "team-aaa-aaa",
        ]
    ]


def test_tag_pane_team_id_empty_is_noop(monkeypatch):
    """Empty team_id is refused (no tmux call) — untagged panes are
    intentionally untouched by cleanup, so writing the empty string
    would be misleading."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    _tmux_workspace.tag_pane_team_id("%5", "")
    assert calls == []


def test_tag_pane_team_id_tolerates_no_server(monkeypatch):
    """Tag is soft-fail when tmux server is not running."""

    def fake_run(argv, **kwargs):
        return _mk_proc(1, "", "no server running on /tmp/tmux-1000/default")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    # Must not raise.
    _tmux_workspace.tag_pane_team_id("%5", "team-x")


def test_apply_workspace_layout_tags_every_pane_with_team_id(monkeypatch):
    """When ``team_id`` is supplied, every mapped teammate pane is
    tagged with ``@kaizen_team_id=<team_id>`` via set-option.
    """
    set_option_calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        if "list-panes" in argv:
            return _mk_proc(0, "%1\n%2\n%3\n")
        if "set-option" in argv and "@kaizen_team_id" in argv:
            set_option_calls.append(list(argv))
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    _tmux_workspace.apply_workspace_layout(
        workspace_name="w",
        ordered_agents=["arch-1", "be-1", "sdet-1"],
        team_id="team-zzz-zzz",
    )
    # Three set-option calls, one per mapped pane.
    panes_tagged = sorted(call[call.index("-t") + 1] for call in set_option_calls)
    assert panes_tagged == ["%1", "%2", "%3"]
    # Each tag carries the same team_id.
    team_ids = {call[-1] for call in set_option_calls}
    assert team_ids == {"team-zzz-zzz"}


def test_apply_workspace_layout_no_team_id_no_tag(monkeypatch):
    """Backwards compat: when ``team_id`` is omitted (legacy callers),
    no @kaizen_team_id tag is written.
    """
    set_option_calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        if "list-panes" in argv:
            return _mk_proc(0, "%1\n%2\n")
        if "set-option" in argv and "@kaizen_team_id" in argv:
            set_option_calls.append(list(argv))
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    _tmux_workspace.apply_workspace_layout(
        workspace_name="w",
        ordered_agents=["arch-1", "be-1"],
        # team_id omitted intentionally.
    )
    assert set_option_calls == []


def test_apply_workspace_layout_does_not_tag_orchestrator_pane(monkeypatch):
    """The orchestrator's own pane (``TMUX_PANE``) is excluded from the
    tagging loop — the orchestrator outlives the team it created.
    """
    monkeypatch.setenv("TMUX_PANE", "%9")
    set_option_calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        if "list-panes" in argv:
            # list-panes already excludes %9 (the orchestrator) — see
            # _list_pane_ids. So pane_to_agent will only contain %1+%2.
            return _mk_proc(0, "%1\n%2\n")
        if "set-option" in argv and "@kaizen_team_id" in argv:
            set_option_calls.append(list(argv))
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    _tmux_workspace.apply_workspace_layout(
        workspace_name="w",
        ordered_agents=["arch-1", "be-1"],
        team_id="team-z",
    )
    panes_tagged = sorted(call[call.index("-t") + 1] for call in set_option_calls)
    # %9 (orchestrator) is NOT in the list — it was excluded at source
    # by _list_pane_ids and the tagging loop also defends against any
    # future leak via the explicit `if pid == lead_pane_id: continue`.
    assert "%9" not in panes_tagged
    assert panes_tagged == ["%1", "%2"]
