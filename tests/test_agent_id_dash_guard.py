"""Structural tests for the `--` end-of-options guard (kaizen#82).

Both ``team_executor`` and ``cleanup_orphans`` match teammate processes by
a regex that BEGINS with ``--agent-id``. Without a literal ``"--"`` argv
element before the pattern, getopt in ``pgrep``/``pkill`` parses the
pattern as an unknown long option and the match silently fails (cleanup
degrades to a no-op). These tests capture the real argv passed to
``subprocess.run`` at all three call sites and assert the guard is present
and correctly positioned — i.e. ``"--"`` immediately precedes the pattern,
and the pattern is the final operand.
"""

from __future__ import annotations

from unittest import mock

from scripts import cleanup_orphans as co
from scripts import team_executor as tx
from scripts.agent_id_match import (
    guarded_argv,
    substring_agent_id_regex,
    team_agent_id_regex,
)


def _assert_dash_guards_pattern(argv: list[str]) -> None:
    """Assert ``argv`` contains ``--`` immediately before the final pattern,
    and the pattern is a real ``--agent-id`` regex (would trip getopt without
    the guard)."""
    assert "--" in argv, f"missing end-of-options guard in argv: {argv}"
    dash_idx = argv.index("--")
    # The guard must be the SECOND-TO-LAST element: `[..., "--", pattern]`.
    assert dash_idx == len(argv) - 2, f"'--' not immediately before pattern: {argv}"
    pattern = argv[-1]
    assert pattern.startswith("--agent-id"), (
        f"pattern does not start with --agent-id (guard would be pointless): {pattern!r}"
    )


# -------------------------------------------------------------------------
# guarded_argv — the single enforcement point.
# -------------------------------------------------------------------------


def test_guarded_argv_inserts_dash_before_pattern():
    argv = guarded_argv("pgrep", ["-f"], "--agent-id foo@bar")
    assert argv == ["pgrep", "-f", "--", "--agent-id foo@bar"]
    _assert_dash_guards_pattern(argv)


def test_guarded_argv_preserves_multiple_flags_in_order():
    argv = guarded_argv("pkill", ["-TERM", "-f"], "--agent-id x@y")
    assert argv == ["pkill", "-TERM", "-f", "--", "--agent-id x@y"]
    _assert_dash_guards_pattern(argv)


# -------------------------------------------------------------------------
# Regex helpers — match intent preserved (not unified).
# -------------------------------------------------------------------------


def test_team_agent_id_regex_anchored_and_escaped():
    # Anchored on literal-space-or-end; team_name re.escape'd.
    assert team_agent_id_regex("kaizen-cycle-5-1") == r"--agent-id \S+@kaizen\-cycle\-5\-1( |$)"
    assert team_agent_id_regex("x") == r"--agent-id \S+@x( |$)"


def test_substring_agent_id_regex_is_substring_match():
    assert substring_agent_id_regex("kaizen-cycle-7") == r"--agent-id\s+\S*kaizen-cycle-7"


# -------------------------------------------------------------------------
# Call site 1 — team_executor._pgrep_teammates (`pgrep -f`).
# -------------------------------------------------------------------------


def test_pgrep_teammates_argv_has_dash_guard():
    fake = mock.Mock(returncode=1, stdout="", stderr="")
    with mock.patch.object(tx.subprocess, "run", return_value=fake) as m_run:
        tx._pgrep_teammates("kaizen-cycle-5-1")
    argv = m_run.call_args.args[0]
    assert argv[:3] == ["pgrep", "-f", "--"]
    _assert_dash_guards_pattern(argv)


# -------------------------------------------------------------------------
# Call site 2 — team_executor._pkill_teammates (`pkill <signal> -f`).
# -------------------------------------------------------------------------


def test_pkill_teammates_argv_has_dash_guard():
    fake = mock.Mock(returncode=1, stdout="", stderr="")
    with mock.patch.object(tx.subprocess, "run", return_value=fake) as m_run:
        tx._pkill_teammates("kaizen-cycle-5-1", "-TERM")
    argv = m_run.call_args.args[0]
    assert argv[:4] == ["pkill", "-TERM", "-f", "--"]
    _assert_dash_guards_pattern(argv)


# -------------------------------------------------------------------------
# Call site 3 — cleanup_orphans._pgrep_agent_processes (`pgrep -af`).
# -------------------------------------------------------------------------


def test_cleanup_orphans_pgrep_argv_has_dash_guard():
    fake = mock.Mock(returncode=1, stdout="", stderr="")
    with mock.patch.object(co.subprocess, "run", return_value=fake) as m_run:
        co._pgrep_agent_processes("kaizen-cycle-5-1")
    argv = m_run.call_args.args[0]
    assert argv[:3] == ["pgrep", "-af", "--"]
    _assert_dash_guards_pattern(argv)
