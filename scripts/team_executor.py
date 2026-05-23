"""Team agent mode cycle executor.

Provides `team_cycle_executor(clone_dir, project, run_row, cycle_n)` — a
drop-in replacement for the subagent-based cycle executor that coordinates
agents via the Agent Teams API (TeamCreate / SendMessage / TeamDelete tools)
rather than one-shot fire-and-forget subagent calls.

# Design

Team agent mode uses a persistent named team scoped to one cycle.  The team
is created at the start of the cycle, participants are sent their briefings
via SendMessage, and the team is torn down (TeamDelete) before this function
returns — whether the cycle succeeded or was abandoned.

# Availability guard

`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` must be set in the environment.
If it is absent, `team_cycle_executor` raises `TeamToolsUnavailableError`
immediately so the caller can surface a clear error rather than silently
falling back to subagent mode.

# Interface

This module intentionally mirrors `scripts.cycle.execute_cycle`'s signature
and outcome contract so `orchestrate_run` can select the executor via a
single `mode` parameter with no other changes.

Outcome dict (matches internal/cycle/SKILL.md):

    # Success
    {
        "status": "success",
        "subject": str | None,
        "commit_sha": str,
        "minutes_memex_slug": str,
        "participants": list[str],
    }

    # Abandoned
    {
        "status": "abandoned",
        "subject": str | None,
        "participants": list[str],
        "phase_reached": "agenda" | "meeting" | "implementation" | "test",
        "reason": "no_consensus" | "destructive_rejected" | "tests_unrecoverable" | "other",
        "detail": str,
        "artifacts": list[str],
    }
"""

from __future__ import annotations

import os
from pathlib import Path


class TeamToolsUnavailableError(RuntimeError):
    """Raised when the Agent Teams API is not available in the current session.

    Set CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 in the environment and ensure
    TeamCreate / SendMessage / TeamDelete tools are enabled before using
    team agent mode.
    """


def _check_team_tools_available() -> None:
    """Raise TeamToolsUnavailableError if the Agent Teams env var is absent.

    This is a fast pre-flight guard — it does not attempt to call the tools,
    only checks for the environment variable that enables them.  A missing
    var means the tools will not be surfaced in the session, so trying to
    invoke them would produce an obscure ToolNotFound error deep in the cycle.
    Failing loudly here produces a human-readable message instead.
    """
    flag = os.environ.get("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", "")
    if flag.strip() not in ("1", "true", "True", "TRUE", "yes", "YES"):
        raise TeamToolsUnavailableError(
            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS is not set (or not truthy). "
            "Set it to '1' to enable the Agent Teams API tools "
            "(TeamCreate, SendMessage, TeamDelete) before running kaizen in "
            "team agent mode.\n"
            "  export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1\n"
            "Or switch to subagent mode: kaizen:improve <url> --mode subagent"
        )


def team_cycle_executor(
    clone_dir: Path,
    project: dict,
    run_row: dict,
    cycle_n: int,
) -> dict:
    """Cycle executor that coordinates agents via the Agent Teams API.

    This function is the team-mode drop-in for `scripts.cycle.execute_cycle`.
    It raises `NotImplementedError` because the real agent coordination
    (TeamCreate / SendMessage / TeamDelete) must happen inside an active
    Claude Code session where those tools are available — Python subprocess
    calls cannot invoke Claude Code tool APIs directly.

    The agent following `internal/cycle/SKILL.md` invokes this executor
    implicitly via its own tool calls; `orchestrate_run` selects this
    executor when `mode='team'` so that tests can inject a fake
    `cycle_executor` callable against the same interface.

    Real usage: the agent running the cycle calls TeamCreate, dispatches
    SendMessage briefings to each participant, waits for responses, and
    calls TeamDelete on teardown — all in the same Claude Code session.
    The *outcome dict* is then returned here so the orchestrator can record
    the cycle result in the DB.

    # Why NotImplementedError, not a no-op?

    A silent no-op would let the orchestrator record a spurious "success"
    cycle with no commit.  Failing loudly ensures the operator notices that
    team mode requires agent-level invocation (not Python subprocess) and
    injects a real executor for testing.
    """
    _check_team_tools_available()
    raise NotImplementedError(
        "Team cycle execution requires an active Claude Code session with "
        "TeamCreate / SendMessage / TeamDelete tools available. "
        "This function is a typed stub — the real executor is the agent "
        "following internal/cycle/SKILL.md with mode='team'. "
        "Inject a fake cycle_executor in tests."
    )
