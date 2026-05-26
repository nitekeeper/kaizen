"""Regression tests for kaizen#64 — pane title persists across activation.

Symptom (kaizen#64): a teammate pane's title was correctly set by the
per-spawn retitle hook (PR #58), but reverted to ``general-purpose`` when
the pane became active — e.g., when CC team-mode activated that teammate
to deliver a message. The corrected title did not stick.

Root cause: tmux 3.4 honors OSC 2 (``ESC ] 2 ; <title> BEL``) emitted by
the pane process unconditionally and overwrites ``pane_title``. The
``allow-rename`` option only gates the legacy escape-k window-rename, not
OSC 2 pane titles. CC's subagent process emits OSC 2 ``general-purpose``
on activation/redraw, clobbering whatever we set via ``select-pane -T``.

Fix (kaizen#64): store the authoritative title in the pane's
``@desired_title`` user-option, which OSC 2 cannot touch, and render
THAT in ``pane-border-format``. The user-visible border keeps the
wave/role label even when ``pane_title`` flickers back to
``general-purpose`` in tmux's internal state.

These tests verify:
  1. ``set_pane_title`` writes ``@desired_title`` (mocked, fast).
  2. ``set_pane_titles`` writes ``@desired_title`` for every pane (mocked).
  3. End-to-end against a real tmux server: after we set the title via
     ``set_pane_title``, an OSC 2 ``general-purpose`` from inside the pane
     does NOT change what the border renders.
  4. The ``CONFIG_BLOCK`` in scripts/_tmux_config.py renders
     ``@desired_title`` with a fallback to ``pane_title``.
"""

from __future__ import annotations

import shutil
import subprocess
import time

import pytest

from scripts import _tmux_config, _tmux_workspace

_SOCKET = "kaizen-test-tmux-persist"
_SESSION = "kaizen-test-persist-session"


def _mk_proc(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# ── Mocked unit tests (no real tmux required) ──────────────────────────────


def test_set_pane_title_persists_desired_title_user_option(monkeypatch):
    """kaizen#64: set_pane_title fires a ``set-option -p @desired_title`` call."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    _tmux_workspace.set_pane_title("%3", "[w2] backend-engineer-1")

    set_opt_calls = [c for c in calls if "set-option" in c and "@desired_title" in c]
    assert len(set_opt_calls) == 1, f"expected 1 set-option -p call, got: {calls}"
    sa = set_opt_calls[0]
    # tmux set-option -p -t %3 @desired_title '<sanitized>'
    assert "-p" in sa, f"must be pane-scoped (-p): {sa}"
    assert sa[sa.index("-t") + 1] == "%3"
    assert sa[sa.index("@desired_title") + 1] == "[w2] backend-engineer-1"


def test_set_pane_titles_persists_desired_title_for_every_pane(monkeypatch):
    """kaizen#64: bulk titler writes @desired_title for each pane in the dict."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    _tmux_workspace.set_pane_titles(
        "w",
        {
            "%1": "[w1] arch-1",
            "%2": "[w1] be-1",
        },
    )
    set_opt_calls = [c for c in calls if "set-option" in c and "@desired_title" in c]
    pane_to_value = {c[c.index("-t") + 1]: c[c.index("@desired_title") + 1] for c in set_opt_calls}
    assert pane_to_value == {
        "%1": "[w1] arch-1",
        "%2": "[w1] be-1",
    }


def test_set_pane_title_persist_happens_before_select_pane(monkeypatch):
    """kaizen#64: @desired_title is persisted BEFORE select-pane -T fires.

    Ordering matters: if select-pane -T fires first and CC's process
    immediately emits OSC 2 to overwrite pane_title, the user would see
    general-purpose for a frame before @desired_title is set. Persisting
    first means the border has the authoritative value from the moment
    pane_title changes.
    """
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    _tmux_workspace.set_pane_title("%5", "[w3] sdet-1")
    # First tmux call must be set-option @desired_title, not select-pane.
    assert "set-option" in calls[0]
    assert "@desired_title" in calls[0]
    assert "select-pane" in calls[1]


def test_persist_desired_title_tolerates_no_server(monkeypatch):
    """No tmux server → silent return, no exception, no stderr noise."""

    def fake_run(argv, **kwargs):
        return _mk_proc(1, "", "no server running on /tmp/tmux-1000/default")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    # Must not raise.
    _tmux_workspace._persist_desired_title("%1", "[w1] x")


def test_persist_desired_title_sanitized_value_only(monkeypatch):
    """The persisted value is sanitized: # → ##, ESC stripped, bidi stripped."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _mk_proc(0, "")

    monkeypatch.setattr(_tmux_workspace.subprocess, "run", fake_run)
    rlo = chr(0x202E)
    _tmux_workspace.set_pane_title("%1", f"[w1]\x1b my#role{rlo}")
    set_opt_calls = [c for c in calls if "set-option" in c and "@desired_title" in c]
    assert len(set_opt_calls) == 1
    persisted = set_opt_calls[0][set_opt_calls[0].index("@desired_title") + 1]
    # ESC stripped, bidi stripped, # escaped to ##.
    assert persisted == "[w1] my##role"
    assert "\x1b" not in persisted
    assert rlo not in persisted


# ── CONFIG_BLOCK shape ─────────────────────────────────────────────────────


def test_config_block_renders_desired_title_with_pane_title_fallback():
    """kaizen#64: pane-border-format MUST render @desired_title with fallback.

    The format string must be the conditional ``#{?@desired_title,
    #{@desired_title},#{pane_title}}`` so that:
      - panes that have been titled by kaizen show the @desired_title
        (immune to OSC 2 overrides);
      - panes without @desired_title set (e.g. plain shell panes outside
        the team workspace) still show their native pane_title.
    """
    block = _tmux_config.CONFIG_BLOCK
    assert "pane-border-format" in block
    # The conditional must reference @desired_title in BOTH the predicate
    # and the true-branch, and pane_title in the false-branch.
    assert "@desired_title" in block, "@desired_title must appear in pane-border-format"
    assert "#{?@desired_title" in block, "format must be conditional on @desired_title (kaizen#64)"
    assert "#{pane_title}" in block, "pane_title fallback must remain for non-team panes"


def test_marker_version_bumped_for_kaizen_64():
    """kaizen#64: MARKER_VERSION must be ≥2 — v1 lacked @desired_title routing.

    Operators with an existing v1 block in ~/.tmux.conf will get prompted
    by setup.py to upgrade; without the bump, they would keep the broken
    pane-border-format rendering plain pane_title (and the bug would not
    fix on existing installs).
    """
    assert _tmux_config.MARKER_VERSION >= 2, (
        f"MARKER_VERSION must be bumped for kaizen#64 (got {_tmux_config.MARKER_VERSION})"
    )


# ── Real-tmux integration test ─────────────────────────────────────────────


pytestmark = pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not on PATH")


def _tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tmux", "-L", _SOCKET, *args], capture_output=True, text=True, check=check
    )


def _kill_server() -> None:
    subprocess.run(
        ["tmux", "-L", _SOCKET, "kill-server"], capture_output=True, text=True, check=False
    )


@pytest.fixture
def routed_persist_tmux(monkeypatch):
    """Spin up a dedicated tmux server on ``_SOCKET`` and route production
    code's ``_run_tmux`` through it. Always tears down in finally.

    Clears TMUX_PANE (kaizen#66) so the orchestrator-exclusion logic does
    not drop a teammate pane that happens to share the developer's outer
    TMUX_PANE id.
    """
    _kill_server()
    monkeypatch.delenv("TMUX_PANE", raising=False)
    try:
        original = _tmux_workspace._run_tmux

        def routed(argv: list[str]) -> subprocess.CompletedProcess:
            return original(["-L", _SOCKET, *argv])

        monkeypatch.setattr(_tmux_workspace, "_run_tmux", routed)
        yield
    finally:
        _kill_server()


def _spawn_session(n_panes: int) -> list[str]:
    _tmux("new-session", "-d", "-s", _SESSION, "-x", "200", "-y", "50", "sh", "-c", "sleep 600")
    for _ in range(n_panes - 1):
        _tmux("split-window", "-d", "-t", _SESSION, "sh", "-c", "sleep 600")
    _tmux("select-layout", "-t", _SESSION, "tiled")
    proc = _tmux("list-panes", "-t", _SESSION, "-F", "#{pane_id}")
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def test_border_renders_desired_title_after_pane_title_override(routed_persist_tmux):
    """End-to-end: an OSC-style override of pane_title MUST NOT change the border.

    Simulates kaizen#64's failure mode at the real tmux layer:
      1. We set the pane title via set_pane_title (persists @desired_title
         + sets pane_title).
      2. We install the agent-teams pane-border-format (renders
         @desired_title with fallback to pane_title).
      3. We manually overwrite pane_title to 'general-purpose' — this
         simulates what CC's subagent process does on activation via OSC 2.
      4. We render the format and assert the OUTPUT is the @desired_title,
         NOT 'general-purpose'.

    Without the fix, the format ``#{pane_title}`` would render
    'general-purpose' and the test fails.
    """
    pane_ids = _spawn_session(n_panes=2)
    assert len(pane_ids) >= 1
    target = pane_ids[0]

    # Install the agent-teams pane-border-format on the test server.
    # We use set-option -g (global to this server) so it applies to all
    # panes including ours. This mirrors what scripts/setup.py does on a
    # user's ~/.tmux.conf via apply_config_block.
    _tmux(
        "set-option",
        "-g",
        "pane-border-format",
        "#{?@desired_title,#{@desired_title},#{pane_title}}",
    )

    # 1. Our authoritative set: persists @desired_title and sets pane_title.
    _tmux_workspace.set_pane_title(target, "[w2] backend-engineer-1")
    time.sleep(0.05)

    # 2. Simulate CC's OSC 2 override: change pane_title behind our back.
    _tmux("select-pane", "-t", target, "-T", "general-purpose")
    time.sleep(0.05)

    # 3. Read what the border WOULD render via display-message with the
    #    same format string.
    rendered = _tmux(
        "display-message",
        "-p",
        "-t",
        target,
        "#{?@desired_title,#{@desired_title},#{pane_title}}",
    ).stdout.strip()
    # The pane_title is now 'general-purpose' (sanity check).
    pane_title_now = _tmux("display-message", "-p", "-t", target, "#{pane_title}").stdout.strip()
    assert pane_title_now == "general-purpose", (
        f"sanity precondition: pane_title should be overridden to "
        f"'general-purpose', got: {pane_title_now!r}"
    )
    # ...but the border-format output should STILL be our wave/role label.
    assert rendered == "[w2] backend-engineer-1", (
        f"kaizen#64 regression: border-format rendered {rendered!r} after "
        "pane_title was overridden — @desired_title fallback did not take "
        "effect. Confirm CONFIG_BLOCK pane-border-format uses "
        "#{?@desired_title,#{@desired_title},#{pane_title}}."
    )


def test_border_format_falls_back_to_pane_title_for_non_kaizen_panes(routed_persist_tmux):
    """kaizen#72.3: pane-border-format must NOT clobber operator's pane_title.

    The fallback ``#{?@desired_title,#{@desired_title},#{pane_title}}``
    is designed so that panes WITHOUT ``@desired_title`` set (e.g. the
    operator's plain shell panes that share the same tmux server but
    are not part of the kaizen team workspace) render their native
    ``pane_title`` unchanged.

    Setup mirrors a shared tmux session: two panes, one titled by
    kaizen (gets ``@desired_title``), one titled the regular way
    (``select-pane -T``, leaves ``@desired_title`` unset). The format
    must render each pane's intended label without collision.
    """
    pane_ids = _spawn_session(n_panes=2)
    assert len(pane_ids) == 2
    kaizen_pane, operator_pane = pane_ids

    # Install the agent-teams pane-border-format on this server (mirrors
    # what scripts/setup.py writes to ~/.tmux.conf via apply_config_block).
    _tmux(
        "set-option",
        "-g",
        "pane-border-format",
        "#{?@desired_title,#{@desired_title},#{pane_title}}",
    )

    # 1. kaizen-managed pane: production path sets BOTH @desired_title
    #    and pane_title.
    _tmux_workspace.set_pane_title(kaizen_pane, "[w1] backend-engineer-1")

    # 2. operator-managed pane: regular tmux title — NO @desired_title
    #    user-option (the operator never asked kaizen to manage it).
    _tmux("select-pane", "-t", operator_pane, "-T", "operator-shell")
    # Defensive: explicitly ensure @desired_title is unset on this pane
    # (in case any test order pollution sets it).
    _tmux("set-option", "-pu", "-t", operator_pane, "@desired_title", check=False)
    time.sleep(0.05)

    # Sanity precondition: @desired_title is set on the kaizen pane
    # only.
    kaizen_desired = _tmux(
        "show-options", "-p", "-t", kaizen_pane, "-v", "@desired_title"
    ).stdout.strip()
    assert kaizen_desired == "[w1] backend-engineer-1", (
        f"setup precondition: kaizen pane should have @desired_title set, got: {kaizen_desired!r}"
    )
    operator_desired = _tmux(
        "show-options", "-p", "-t", operator_pane, "-v", "@desired_title", check=False
    ).stdout.strip()
    assert operator_desired == "", (
        "setup precondition: operator pane should NOT have @desired_title set, "
        f"got: {operator_desired!r}"
    )

    # Render the border format for each pane.
    kaizen_rendered = _tmux(
        "display-message",
        "-p",
        "-t",
        kaizen_pane,
        "#{?@desired_title,#{@desired_title},#{pane_title}}",
    ).stdout.strip()
    operator_rendered = _tmux(
        "display-message",
        "-p",
        "-t",
        operator_pane,
        "#{?@desired_title,#{@desired_title},#{pane_title}}",
    ).stdout.strip()

    # The kaizen pane shows the @desired_title value.
    assert kaizen_rendered == "[w1] backend-engineer-1", (
        f"kaizen pane should render @desired_title; got: {kaizen_rendered!r}"
    )
    # The operator pane shows its regular pane_title — NOT clobbered.
    assert operator_rendered == "operator-shell", (
        "kaizen#72.3 regression: operator pane (no @desired_title) "
        "should render its native pane_title via the fallback, but got: "
        f"{operator_rendered!r}. Confirm the format string is "
        "#{?@desired_title,#{@desired_title},#{pane_title}} — a missing "
        "fallback would clobber non-kaizen panes."
    )


def test_set_pane_title_persists_across_pane_activation(routed_persist_tmux):
    """End-to-end: after select-pane (activation), @desired_title still holds.

    Activating a pane via select-pane fires tmux's after-select-pane hooks
    and gives the pane process focus. We assert that AFTER activation
    @desired_title is still our value (user-options are not touched by
    OSC 2 or focus events).
    """
    pane_ids = _spawn_session(n_panes=2)
    target = pane_ids[0]
    other = pane_ids[1]

    _tmux_workspace.set_pane_title(target, "[w1] sdet-1")
    time.sleep(0.05)

    # Activate the other pane, then activate ours back (simulates CC's
    # round-robin focus behavior across teammates).
    _tmux("select-pane", "-t", other)
    _tmux("select-pane", "-t", target)
    time.sleep(0.05)

    # @desired_title MUST still hold our value.
    desired = _tmux("show-options", "-p", "-t", target, "-v", "@desired_title").stdout.strip()
    assert desired == "[w1] sdet-1", (
        f"@desired_title was lost across pane activation (kaizen#64 regression). Got: {desired!r}"
    )
