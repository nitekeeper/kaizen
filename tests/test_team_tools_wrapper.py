"""Tests for scripts/team_tools_wrapper.py — production TeamTools surface.

These tests verify:
  - the base AgentTeamsWrapper raises NotImplementedInThisRuntime with
    actionable subclass-and-override guidance (Python cannot call CC
    session tools)
  - RecordingWrapper records the full lifecycle in order and honors the
    scripted responses callable
  - RecordingWrapper structurally implements the TeamTools Protocol —
    typing.Protocol is not runtime-checkable by default so we check the
    three required methods via hasattr
"""

from __future__ import annotations

import pytest

from scripts.team_tools_wrapper import (
    AgentTeamsWrapper,
    NotImplementedInThisRuntime,
    RecordingWrapper,
)

# ── AgentTeamsWrapper base class ─────────────────────────────────────────


def test_agent_teams_wrapper_team_create_raises_NotImplementedInThisRuntime():
    w = AgentTeamsWrapper()
    with pytest.raises(NotImplementedInThisRuntime) as exc:
        w.team_create("cycle-1", ["pm-1", "be-1"])
    assert "subclass" in str(exc.value).lower()


def test_agent_teams_wrapper_send_message_raises_NotImplementedInThisRuntime():
    w = AgentTeamsWrapper()
    with pytest.raises(NotImplementedInThisRuntime) as exc:
        w.send_message("team-xyz", "pm-1", "hello")
    assert "subclass" in str(exc.value).lower()


def test_agent_teams_wrapper_team_delete_raises_NotImplementedInThisRuntime():
    w = AgentTeamsWrapper()
    with pytest.raises(NotImplementedInThisRuntime) as exc:
        w.team_delete("team-xyz")
    assert "subclass" in str(exc.value).lower()


# ── RecordingWrapper ─────────────────────────────────────────────────────


def test_recording_wrapper_records_lifecycle_calls():
    w = RecordingWrapper()
    team_id = w.team_create("c-1", ["pm-1", "be-1"])
    w.send_message(team_id, "pm-1", "Phase 1 brief")
    w.send_message(team_id, "be-1", "Phase 4 brief")
    w.team_delete(team_id)

    op_names = [c[0] for c in w.calls]
    assert op_names == ["team_create", "send_message", "send_message", "team_delete"]
    # Confirm the team_id contract: f"team-{name}" survives into delete
    assert team_id == "team-c-1"
    assert w.calls[-1] == ("team_delete", ("team-c-1",), {})


def test_recording_wrapper_uses_scripted_responses():
    def responder(to: str, message: str) -> str:
        if to == "pm-1":
            return "agenda item A"
        if "Phase 4" in message:
            return "applied change"
        return "ack"

    w = RecordingWrapper(responses=responder)
    tid = w.team_create("c", ["pm-1", "be-1"])
    assert w.send_message(tid, "pm-1", "Phase 1") == "agenda item A"
    assert w.send_message(tid, "be-1", "Phase 4 wave 1") == "applied change"
    assert w.send_message(tid, "be-1", "anything else") == "ack"


def test_recording_wrapper_implements_TeamTools_protocol():
    # typing.Protocol is not runtime-checkable by default — check the three
    # required methods via hasattr + callable.
    w = RecordingWrapper()
    for method_name in ("team_create", "send_message", "team_delete"):
        attr = getattr(w, method_name, None)
        assert attr is not None, f"RecordingWrapper missing {method_name}"
        assert callable(attr), f"RecordingWrapper.{method_name} is not callable"
