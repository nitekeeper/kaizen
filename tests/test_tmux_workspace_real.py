"""Real-tmux integration tests for scripts/_tmux_workspace.py.

These tests exercise the production code paths against a REAL tmux
server — not a mocked ``subprocess.run``. The all-mocked
``tests/test_tmux_workspace.py`` happily accepts canned returns
regardless of the argv shape, which is exactly why the kaizen#61 bug
("workspace_name passed as ``-t`` to a non-existent tmux target")
shipped in PR#58 (mocks-must-match-reality, per the memory note).

Isolation strategy:
  - Dedicated tmux socket via ``-L kaizen-test`` — this never touches
    the user's live tmux server, so a developer running ``pytest``
    inside their normal tmux session is safe.
  - Production code's ``_run_tmux`` is monkeypatched to PREPEND
    ``-L kaizen-test`` to every argv so all production calls route to
    the dedicated server.
  - Setup creates a single detached session containing N panes; the
    finally-block kills the server.

Gated by ``@pytest.mark.skipif(not shutil.which("tmux"))`` so CI hosts
without tmux installed (the project's GitHub Actions runner is
generally bare) still pass — these tests are best-effort
defense-in-depth, not the primary coverage source.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time

import pytest

from scripts import _tmux_workspace

_SOCKET = "kaizen-test-tmux"
_SESSION = "kaizen-test-session"

pytestmark = pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not on PATH")


def _tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a tmux command against the dedicated test socket."""
    return subprocess.run(
        ["tmux", "-L", _SOCKET, *args],
        capture_output=True,
        text=True,
        check=check,
    )


def _kill_server() -> None:
    """Best-effort: tear down the dedicated server. No-op if not running."""
    subprocess.run(
        ["tmux", "-L", _SOCKET, "kill-server"],
        capture_output=True,
        text=True,
        check=False,
    )


def _spawn_session(n_panes: int) -> list[str]:
    """Create a detached session with ``n_panes`` panes; return their pane_ids
    in tmux's positional list order (left-to-right, top-to-bottom)."""
    # Start the session with one initial pane.
    _tmux(
        "new-session",
        "-d",
        "-s",
        _SESSION,
        "-x",
        "200",
        "-y",
        "50",
        "sh",
        "-c",
        "sleep 600",
    )
    # Split it ``n_panes - 1`` more times.
    for _ in range(n_panes - 1):
        _tmux("split-window", "-d", "-t", _SESSION, "sh", "-c", "sleep 600")
    # Reset to the default tiled layout so the positional list is
    # predictable across tmux versions.
    _tmux("select-layout", "-t", _SESSION, "tiled")
    # Read back the pane_ids in positional order.
    proc = _tmux("list-panes", "-t", _SESSION, "-F", "#{pane_id}")
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


@pytest.fixture
def routed_tmux(monkeypatch):
    """Monkeypatch ``_run_tmux`` to route through ``-L kaizen-test`` and
    spawn the test session. Always tears down the server in finally.

    The production code calls ``_run_tmux`` without any socket flag,
    relying on tmux's default socket; this fixture transparently
    redirects every call to the dedicated test server.
    """
    _kill_server()  # paranoia — kill any leftover from a prior crash
    try:
        original = _tmux_workspace._run_tmux

        def routed(argv: list[str]) -> subprocess.CompletedProcess:
            return original(["-L", _SOCKET, *argv])

        monkeypatch.setattr(_tmux_workspace, "_run_tmux", routed)
        yield
    finally:
        _kill_server()


def test_apply_workspace_layout_against_real_tmux(routed_tmux):
    """End-to-end: layout + titles applied to a real tmux session.

    Verifies kaizen#61 stays fixed: the production code drops ``-t
    workspace_name`` from list-panes / select-layout, so a non-existent
    "team_name" target does NOT cause every tmux call to silently
    no-op. With four real panes on the test server, the function must
    successfully list them and apply the main-vertical layout.
    """
    pane_ids = _spawn_session(n_panes=4)
    assert len(pane_ids) == 4, f"setup precondition: got {pane_ids}"

    # Sanity: confirm the test socket has the session we think it does.
    out = _tmux("list-sessions", "-F", "#{session_name}").stdout
    assert _SESSION in out

    # Note: workspace_name is set to a bogus value to prove the
    # production code does NOT use it as a tmux target. If it did, this
    # call would silently no-op (the kaizen#61 bug we're testing the
    # absence of).
    result = _tmux_workspace.apply_workspace_layout(
        workspace_name="not-a-real-tmux-target",
        ordered_agents=["arch-1", "be-1", "sdet-1", "sec-1"],
    )
    # Positional zip from the live list-panes output.
    assert len(result) == 4
    assert set(result.values()) == {"arch-1", "be-1", "sdet-1", "sec-1"}

    # R1-1 (Phase 5b' major): verify the 2-column grid actually
    # MATERIALIZED at the tmux layer, not just that the function
    # returned a pane-to-agent map. Without this assertion the test
    # would pass even if every join-pane call silently no-op'd at the
    # real-tmux level — the same mocks-must-match-reality slip class
    # that produced kaizen#61.
    #
    # The ``window_layout`` format string is tmux's compact serialization
    # of pane geometry: panes are grouped into rows/columns with ``{...}``
    # brackets nesting one geometry inside another. A plain
    # ``main-vertical`` layout (1 wide left + N stacked right) produces a
    # SINGLE ``{`` bracket — the outer "main + right column" group. After
    # ``fold_right_column`` joins pairs in the right column, each pair
    # introduces an ADDITIONAL ``{`` bracket: ``{main, {row1pair} {row2pair} ...}``.
    # With 4 panes (1 main + 3 right → one folded pair + one solo), the
    # fold contributes at least one extra bracket — total ≥ 2. If the
    # fold silently fails, the count drops to 1 and this assertion fires.
    layout_out = subprocess.run(
        ["tmux", "-L", _SOCKET, "list-windows", "-t", _SESSION, "-F", "#{window_layout}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert layout_out.count("{") >= 2, (
        "Expected ≥2 brace-groups in window_layout — one for the outer "
        "main-vertical (main + right column), at least one more for the "
        "folded right-column pair. Got: "
        f"{layout_out!r}. If this count is 1, fold_right_column's "
        "join-pane calls silently no-op'd at the real-tmux level — the "
        "2-column grid did not materialize (kaizen#63 regression)."
    )

    # Now title every pane and read back the titles via display-message.
    _tmux_workspace.set_pane_titles(
        "not-a-real-tmux-target",
        {pid: f"[w1] {name}" for pid, name in result.items()},
    )
    # tmux applies pane titles asynchronously on some platforms; a
    # short settle is cheap insurance against flakes.
    time.sleep(0.05)
    for pid, name in result.items():
        out = _tmux("display-message", "-p", "-t", pid, "#T").stdout
        assert f"[w1] {name}" in out, f"expected '[w1] {name}' in title of {pid}, got: {out!r}"


def test_set_pane_title_sanitizes_against_real_tmux(routed_tmux):
    """Sanitizer runs on the real path — ``#`` is escaped, controls stripped."""
    pane_ids = _spawn_session(n_panes=2)
    target = pane_ids[0]

    # A title with a single ``#`` would be interpreted as a tmux format
    # specifier (``#H`` = hostname); the sanitizer escapes ``#`` to
    # ``##`` so tmux prints the literal hash. We also include an ESC
    # byte (would otherwise produce an ANSI escape sequence in the
    # status bar) and a bidi control to confirm both get stripped.
    # Bidi codepoint via ``chr()`` to keep the literal off this source
    # file (bandit B613).
    rlo = chr(0x202E)  # RIGHT-TO-LEFT OVERRIDE
    raw_title = f"[w2] my#role\x1b[31m{rlo}tail"
    _tmux_workspace.set_pane_title(target, raw_title)
    time.sleep(0.05)
    rendered = _tmux("display-message", "-p", "-t", target, "#T").stdout
    # The literal "##" in the sanitized title gets re-rendered by tmux
    # status-line formatting as a single "#" character — that's the
    # whole point of escaping. We assert on the result the user sees.
    assert "#" in rendered  # the literal hash, not a format spec
    assert "\x1b" not in rendered
    assert rlo not in rendered


def test_apply_workspace_layout_no_tmux_server_returns_empty(monkeypatch):
    """When the dedicated server is NOT started, the helper returns ``{}``."""
    # Defensive: ensure no leftover socket.
    _kill_server()
    # Use a randomly-named socket that definitely has no server.
    bogus_socket = f"kaizen-test-bogus-{os.getpid()}"
    original = _tmux_workspace._run_tmux

    def routed(argv):
        return original(["-L", bogus_socket, *argv])

    monkeypatch.setattr(_tmux_workspace, "_run_tmux", routed)
    result = _tmux_workspace.apply_workspace_layout(
        workspace_name="anything", ordered_agents=["a", "b"]
    )
    assert result == {}
