"""Reference AgentTeamsWrapper subclass — the callback-based wiring pattern.

In production, the orchestrating Claude Code session must wire real
TeamCreate / SendMessage / TeamDelete tool calls into an AgentTeamsWrapper
subclass that scripts.run.orchestrate_run can use via the tools_provider
callback.

This module ships the simplest such subclass: it takes 3 callables in its
constructor and just dispatches through them. The orchestrating agent passes
real CC-tool-invoking lambdas. Tests pass mocks.

# Example production usage (in pseudocode — the agent provides real CC
# tool wrappers from its own session context):
#
#   from examples.agent_teams_wrapper_example import CallbackWrapper
#   wrapper = CallbackWrapper(
#       team_create_cb=lambda name, members: TeamCreate(name=name, members=members),
#       send_message_cb=lambda team_id, to, message: SendMessage(team_id=team_id, to=to, message=message),
#       team_delete_cb=lambda team_id: TeamDelete(team_id=team_id),
#   )
#   orchestrate_run(..., mode='team', tools_provider=lambda *a: wrapper)
"""

from __future__ import annotations

from collections.abc import Callable

from scripts.team_tools_wrapper import AgentTeamsWrapper


class CallbackWrapper(AgentTeamsWrapper):
    """AgentTeamsWrapper that dispatches each method through an injected callback.

    Each callback's signature MUST match the underlying TeamTools Protocol method:
      - team_create_cb(name: str, members: list[str]) -> str
      - send_message_cb(team_id: str, to: str, message: str) -> str
      - team_delete_cb(team_id: str) -> None

    Production: the orchestrating agent provides callbacks that invoke the real
    CC session tools (TeamCreate / SendMessage / TeamDelete).
    Tests: provide mock callables.
    """

    def __init__(
        self,
        *,
        team_create_cb: Callable[[str, list[str]], str],
        send_message_cb: Callable[[str, str, str], str],
        team_delete_cb: Callable[[str], None],
    ):
        if not callable(team_create_cb):
            raise TypeError("team_create_cb must be callable")
        if not callable(send_message_cb):
            raise TypeError("send_message_cb must be callable")
        if not callable(team_delete_cb):
            raise TypeError("team_delete_cb must be callable")
        self._team_create_cb = team_create_cb
        self._send_message_cb = send_message_cb
        self._team_delete_cb = team_delete_cb

    def team_create(self, name: str, members: list[str]) -> str:
        return self._team_create_cb(name, members)

    def send_message(self, team_id: str, to: str, message: str) -> str:
        return self._send_message_cb(team_id, to, message)

    def team_delete(self, team_id: str) -> None:
        return self._team_delete_cb(team_id)
