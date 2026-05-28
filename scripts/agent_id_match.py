"""Shared `--agent-id` matching helpers for pgrep/pkill cleanup call sites.

Kaizen identifies live Claude Code teammate processes by matching the
``--agent-id <name>@<team>`` token in their argv via ``pgrep -f`` /
``pkill -f``. Two distinct match intents exist and are preserved here:

  * :func:`team_agent_id_regex` — anchored team-scoped match used by
    ``team_executor`` (``--agent-id \\S+@<team>( |$)``). The
    literal-space-or-end boundary stops ``kaizen-cycle-5-1`` from matching
    ``kaizen-cycle-5-11``.
  * :func:`substring_agent_id_regex` — looser substring match used by
    ``cleanup_orphans`` Layer 1 (``--agent-id\\s+\\S*<pattern>``), where
    ``<pattern>`` is an operator-supplied substring.

THE END-OF-OPTIONS GUARD (kaizen#82). Both regexes BEGIN with the literal
``--agent-id``. When that string is handed to ``pgrep``/``pkill`` as the
pattern positional, the tool's getopt-style parser sees an argv element
starting with ``--`` and tries to interpret it as a (long) option —
``pgrep: unrecognized option '--agent-id ...'`` — so the match never runs
and cleanup silently degrades. The fix is the POSIX end-of-options marker:
a literal ``"--"`` as its own argv element immediately before the pattern.
:func:`guarded_argv` is the single chokepoint that enforces this so no
call site can reintroduce the bug.

NB on ``\\s`` vs ``( |$)``: BSD ``pgrep -f`` does NOT understand ``\\s``;
the anchored team regex deliberately uses ``( |$)`` (kaizen#68 iter 2).
The substring regex keeps ``\\s`` as it predates that finding and targets
the GNU/procps ``pgrep`` on the deployment host; the shared helper unifies
only the ``--`` guard convention, NOT the pattern dialect.
"""

from __future__ import annotations

import re

__all__ = [
    "guarded_argv",
    "substring_agent_id_regex",
    "team_agent_id_regex",
]


def team_agent_id_regex(team_name: str) -> str:
    """Return the anchored ``pgrep``/``pkill`` ``-f`` regex for ``team_name``.

    Matches ``--agent-id <name>@<team_name>`` anchored on a literal-space-or-end
    boundary after the team name so a substring of a longer team name cannot
    match. ``team_name`` is ``re.escape``-d defensively.
    """
    return rf"--agent-id \S+@{re.escape(team_name)}( |$)"


def substring_agent_id_regex(pattern: str) -> str:
    """Return the substring ``pgrep -af`` regex for cleanup Layer 1.

    Requires both ``--agent-id`` AND ``<pattern>`` to appear in the argv.
    ``pattern`` is an operator-supplied substring (see ``cleanup_orphans``);
    it is interpolated as-is to preserve the existing match intent.
    """
    return rf"--agent-id\s+\S*{pattern}"


def guarded_argv(command: str, flags: list[str], pattern: str) -> list[str]:
    """Build a pgrep/pkill argv with the ``--`` end-of-options guard.

    Returns ``[command, *flags, "--", pattern]``. The literal ``"--"`` is the
    POSIX marker that terminates option parsing, so a ``pattern`` beginning
    with ``--agent-id`` is treated as the pattern operand rather than an
    unknown option. This is the single enforcement point for kaizen#82 —
    every pgrep/pkill call site MUST route through here.
    """
    return [command, *flags, "--", pattern]
