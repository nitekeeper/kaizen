"""Tests for examples/agent_teams_wrapper_example.py — CallbackWrapper.

Confirms the reference subclass dispatches through its injected callbacks
correctly and validates each callback's callability with a clear TypeError.
"""

from __future__ import annotations

import pytest

from examples.agent_teams_wrapper_example import CallbackWrapper
from scripts.team_tools_wrapper import AgentTeamsWrapper

# ── Dispatch tests ────────────────────────────────────────────────────────


def test_callback_wrapper_dispatches_team_create():
    calls: list[tuple] = []

    def fake_team_create(name: str, members: list[str]) -> str:
        calls.append((name, list(members)))
        return f"team-id-{name}"

    wrapper = CallbackWrapper(
        team_create_cb=fake_team_create,
        send_message_cb=lambda t, to, m: "ack",
        team_delete_cb=lambda t: None,
    )
    rv = wrapper.team_create("my-team", ["pm-1", "be-1"])
    assert calls == [("my-team", ["pm-1", "be-1"])]
    assert rv == "team-id-my-team", "team_create return value must flow through unchanged"


def test_callback_wrapper_dispatches_send_message():
    calls: list[tuple] = []

    def fake_send(team_id: str, to: str, message: str) -> str:
        calls.append((team_id, to, message))
        return "scripted-response"

    wrapper = CallbackWrapper(
        team_create_cb=lambda n, m: "team-x",
        send_message_cb=fake_send,
        team_delete_cb=lambda t: None,
    )
    rv = wrapper.send_message("team-x", "be-1", "hello")
    assert calls == [("team-x", "be-1", "hello")]
    assert rv == "scripted-response", "send_message return value must flow through unchanged"


def test_callback_wrapper_dispatches_team_delete():
    calls: list[str] = []

    def fake_delete(team_id: str) -> None:
        calls.append(team_id)

    wrapper = CallbackWrapper(
        team_create_cb=lambda n, m: "team-x",
        send_message_cb=lambda t, to, m: "ack",
        team_delete_cb=fake_delete,
    )
    rv = wrapper.team_delete("team-x")
    assert calls == ["team-x"]
    assert rv is None, "team_delete must return None (matches Protocol)"


# ── Callability validation ────────────────────────────────────────────────


def test_callback_wrapper_raises_TypeError_on_noncallable_team_create_cb():
    with pytest.raises(TypeError, match="team_create_cb"):
        CallbackWrapper(
            team_create_cb="not callable",  # type: ignore[arg-type]
            send_message_cb=lambda t, to, m: "ack",
            team_delete_cb=lambda t: None,
        )


def test_callback_wrapper_raises_TypeError_on_noncallable_send_message_cb():
    with pytest.raises(TypeError, match="send_message_cb"):
        CallbackWrapper(
            team_create_cb=lambda n, m: "team-x",
            send_message_cb=42,  # type: ignore[arg-type]
            team_delete_cb=lambda t: None,
        )


def test_callback_wrapper_raises_TypeError_on_noncallable_team_delete_cb():
    with pytest.raises(TypeError, match="team_delete_cb"):
        CallbackWrapper(
            team_create_cb=lambda n, m: "team-x",
            send_message_cb=lambda t, to, m: "ack",
            team_delete_cb=None,  # type: ignore[arg-type]
        )


# ── Protocol shape ────────────────────────────────────────────────────────


def test_callback_wrapper_satisfies_team_tools_protocol():
    """The 3 TeamTools methods must exist + be callable on a constructed instance."""
    wrapper = CallbackWrapper(
        team_create_cb=lambda n, m: "team-x",
        send_message_cb=lambda t, to, m: "ack",
        team_delete_cb=lambda t: None,
    )
    for method_name in ("team_create", "send_message", "team_delete"):
        assert hasattr(wrapper, method_name), f"missing method: {method_name}"
        assert callable(getattr(wrapper, method_name)), f"{method_name} is not callable"
    # And the class is a real AgentTeamsWrapper subclass (so the runtime
    # Protocol shape check in team_cycle_executor accepts it).
    assert isinstance(wrapper, AgentTeamsWrapper)
