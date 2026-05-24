"""Production TeamTools wrapper for team agent mode.

The Agent Teams API (TeamCreate / SendMessage / TeamDelete) is a Claude Code
session-scoped API; Python cannot invoke it directly. This module provides:

  1. AgentTeamsWrapper — a base class implementing the TeamTools Protocol.
     The default methods raise NotImplementedInThisRuntime explaining the
     wrapper must be subclassed by the orchestrating agent, which provides
     real tool-call wrappers from its own session context.

  2. RecordingWrapper — a test/debug wrapper that records every call and
     returns scripted responses. Useful for end-to-end harness tests that
     don't have a live CC session.

The orchestrating agent (running internal/cycle/SKILL.md with mode='team')
subclasses AgentTeamsWrapper and overrides team_create / send_message /
team_delete to call the actual CC tools.
"""

from __future__ import annotations

from collections.abc import Callable


class NotImplementedInThisRuntime(NotImplementedError):
    """Raised by AgentTeamsWrapper's default methods.

    The orchestrating agent must subclass AgentTeamsWrapper and override
    these methods to invoke the actual Claude Code session tools.
    """


class AgentTeamsWrapper:
    """Base implementation of the TeamTools Protocol.

    Production callers (the orchestrating agent) MUST subclass this and
    override team_create / send_message / team_delete with wrappers that
    invoke the real Claude Code session tools (TeamCreate / SendMessage /
    TeamDelete) from their own tool context.

    Default methods raise NotImplementedInThisRuntime with actionable error
    messages — Python cannot directly call CC session tools.
    """

    def team_create(self, name: str, members: list[str]) -> str:
        raise NotImplementedInThisRuntime(
            f"AgentTeamsWrapper.team_create(name={name!r}, members={members}) "
            "called from Python. The orchestrating agent (running "
            "internal/cycle/SKILL.md with mode='team') must subclass "
            "AgentTeamsWrapper and override team_create to invoke the actual "
            "TeamCreate tool from its session context."
        )

    def send_message(self, team_id: str, to: str, message: str) -> str:
        raise NotImplementedInThisRuntime(
            f"AgentTeamsWrapper.send_message(team_id={team_id!r}, to={to!r}, "
            f"message=<{len(message)} chars>) called from Python. Subclass "
            "AgentTeamsWrapper and override send_message."
        )

    def send_message_many(self, messages: list[dict]) -> list[str]:
        """Default batch fan-out — calls send_message N times sequentially.

        Subclasses that want true batched/parallel dispatch (e.g.
        QueueBridgeWrapper's single-transaction enqueue) MUST override
        this. The default is correctness-preserving but loses the
        wall-clock parallelism that motivated this method (GAP-4 of
        docs/kaizen/2026-05-24-bridge-smoke-2.md).
        """
        return [self.send_message(m["team_id"], m["to"], m["message"]) for m in messages]

    def team_delete(self, team_id: str) -> None:
        raise NotImplementedInThisRuntime(
            f"AgentTeamsWrapper.team_delete(team_id={team_id!r}) called from "
            "Python. Subclass AgentTeamsWrapper and override team_delete."
        )


class RecordingWrapper(AgentTeamsWrapper):
    """Test/debug wrapper that records every call and returns scripted responses.

    Useful for harness tests that exercise team_cycle_executor end-to-end
    without a live CC session. Production code should NOT use this.

    Constructor takes a `responses` callable: (recipient, message) -> str.
    When omitted, every send_message returns the literal "ack".
    """

    def __init__(self, responses: Callable[[str, str], str] | None = None):
        self.calls: list[tuple[str, tuple, dict]] = []
        self._responses = responses or (lambda to, message: "ack")

    def team_create(self, name: str, members: list[str]) -> str:
        self.calls.append(("team_create", (name,), {"members": list(members)}))
        return f"team-{name}"

    def send_message(self, team_id: str, to: str, message: str) -> str:
        self.calls.append(("send_message", (team_id, to), {"message": message[:120]}))
        return self._responses(to, message)

    def team_delete(self, team_id: str) -> None:
        self.calls.append(("team_delete", (team_id,), {}))
