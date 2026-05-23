"""Tests for scripts/team_executor.py — team agent mode cycle executor.

These tests use a MockTeamTools fixture to assert the lifecycle invariants
of `team_cycle_executor`:

  - team_create fires first, team_delete fires last, regardless of what
    happens in between (including exceptions raised by send_message)
  - the outcome dict shape matches internal/cycle/SKILL.md (both for
    success and abandonment paths)
  - the abandonment outcome's `phase_reached` and `reason` values are
    members of the canonical frozensets in scripts.abandonment so they
    pass the orchestrator's allowlist guards before any DB write
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from scripts.abandonment import VALID_PHASES, VALID_REASONS
from scripts.team_executor import TeamToolsUnavailableError, team_cycle_executor

# ── MockTeamTools fixture ─────────────────────────────────────────────────


class MockTeamTools:
    """Records every tool call in order so tests can assert lifecycle.

    `send_responses` lets a test pre-program the sequence of responses
    each send_message call returns; the list wraps around if exhausted.
    `raise_on_send_call_n` (1-indexed) makes the Nth send_message raise
    a RuntimeError so tests can prove team_delete still fires.
    """

    def __init__(
        self,
        send_responses: list[str] | None = None,
        raise_on_send_call_n: int | None = None,
    ):
        self.calls: list[tuple[str, tuple, dict]] = []
        self._send_responses = list(send_responses or ["ack"])
        self._response_idx = 0
        self._raise_on_send_call_n = raise_on_send_call_n
        self._send_call_count = 0

    def team_create(self, name, members):
        self.calls.append(("team_create", (name,), {"members": list(members)}))
        return f"team-{name}"

    def send_message(self, team_id, to, message):
        self._send_call_count += 1
        self.calls.append(("send_message", (team_id, to), {"message": message}))
        if self._raise_on_send_call_n == self._send_call_count:
            raise RuntimeError(f"injected send_message failure on call {self._send_call_count}")
        resp = self._send_responses[self._response_idx % len(self._send_responses)]
        self._response_idx += 1
        return resp

    def team_delete(self, team_id):
        self.calls.append(("team_delete", (team_id,), {}))


def _project(roster: list[str] | None = None) -> dict:
    return {
        "name": "test-project",
        "git_url": "https://example.invalid/test.git",
        "expert_roster": roster if roster is not None else ["pm-1", "backend-engineer-1"],
    }


def _run_row(run_id: int = 1, subject: str | None = "test subject") -> dict:
    return {"id": run_id, "subject": subject}


# ── _check_team_tools_available ───────────────────────────────────────────


class TestCheckTeamToolsAvailable:
    """The guard function raises when the env var is absent or falsy."""

    @pytest.mark.parametrize("value", ["1", "true", "True", "TRUE", "yes", "YES"])
    def test_does_not_raise_when_env_truthy(self, value):
        with patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": value}):
            from scripts.team_executor import _check_team_tools_available

            _check_team_tools_available()  # must not raise

    @pytest.mark.parametrize("value", ["0", "false", "False", "", "no"])
    def test_raises_when_env_falsy(self, value):
        with patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": value}):
            from scripts.team_executor import _check_team_tools_available

            with pytest.raises(TeamToolsUnavailableError):
                _check_team_tools_available()

    def test_raises_when_env_absent(self):
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"}
        with patch.dict(os.environ, env, clear=True):
            from scripts.team_executor import _check_team_tools_available

            with pytest.raises(TeamToolsUnavailableError):
                _check_team_tools_available()


# ── team_cycle_executor ───────────────────────────────────────────────────


class TestTeamCycleExecutor:
    """Lifecycle + outcome-shape contract."""

    def test_raises_unavailable_when_env_absent(self, tmp_path):
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"}
        with patch.dict(os.environ, env, clear=True), pytest.raises(TeamToolsUnavailableError):
            team_cycle_executor(
                clone_dir=tmp_path,
                project=_project(),
                run_row=_run_row(),
                cycle_n=1,
                tools=MockTeamTools(),
            )

    def test_raises_unavailable_when_tools_none_even_with_env_set(self, tmp_path):
        """Env-var present but tools=None must still raise — no silent degrade.

        Python cannot directly call Claude Code session tools, so a None
        wrapper is unrecoverable in production. Failing loudly here
        forces the orchestrating agent to wire up the injection.
        """
        with (
            patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}),
            pytest.raises(TeamToolsUnavailableError) as exc_info,
        ):
            team_cycle_executor(
                clone_dir=tmp_path,
                project=_project(),
                run_row=_run_row(),
                cycle_n=1,
                tools=None,
            )
        # The error message must name the injection contract so the operator
        # knows the fix is "inject a TeamTools", not "set another env var".
        msg = str(exc_info.value)
        assert "tools=None" in msg
        assert "TeamTools" in msg

    def test_raises_when_tools_missing_required_method(self, tmp_path):
        """Runtime Protocol check: an object that does not implement the
        full TeamTools surface must be rejected BEFORE team_create fires.

        TeamTools is typing.Protocol (static-only). Without a runtime
        check, a malformed wrapper would blow up mid-cycle with a
        generic AttributeError, AFTER team_create — leaving an orphan
        team behind.
        """

        class PartialTools:
            # Has team_create only — missing send_message and team_delete.
            def team_create(self, name, members):
                return "team-x"

        with (
            patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}),
            pytest.raises(TeamToolsUnavailableError) as exc_info,
        ):
            team_cycle_executor(
                clone_dir=tmp_path,
                project=_project(),
                run_row=_run_row(),
                cycle_n=1,
                tools=PartialTools(),
            )
        # The first missing method (in declaration order) must be named
        # in the error so the operator knows exactly what to add.
        msg = str(exc_info.value)
        assert "send_message" in msg
        assert "missing required method" in msg

        # And a bare object() must also be rejected — fails on team_create,
        # the first method checked.
        with (
            patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}),
            pytest.raises(TeamToolsUnavailableError) as exc_info,
        ):
            team_cycle_executor(
                clone_dir=tmp_path,
                project=_project(),
                run_row=_run_row(),
                cycle_n=1,
                tools=object(),
            )
        assert "team_create" in str(exc_info.value)

    def test_happy_path_calls_team_create_then_send_then_delete_in_order(self, tmp_path):
        tools = MockTeamTools(send_responses=["agenda: do the thing"])
        with patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}):
            team_cycle_executor(
                clone_dir=tmp_path,
                project=_project(),
                run_row=_run_row(),
                cycle_n=1,
                tools=tools,
            )
        names = [c[0] for c in tools.calls]
        assert names[0] == "team_create"
        assert names[-1] == "team_delete"
        assert "send_message" in names
        # No spurious extra phases in the skeleton — exactly one round-trip.
        assert names == ["team_create", "send_message", "team_delete"]

    def test_team_delete_fires_even_when_send_message_raises(self, tmp_path):
        tools = MockTeamTools(raise_on_send_call_n=1)
        with (
            patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}),
            pytest.raises(RuntimeError, match="injected send_message failure"),
        ):
            team_cycle_executor(
                clone_dir=tmp_path,
                project=_project(),
                run_row=_run_row(),
                cycle_n=1,
                tools=tools,
            )
        # The CRITICAL invariant: team_delete must still appear in calls
        # even when send_message blew up.
        names = [c[0] for c in tools.calls]
        assert names[-1] == "team_delete", (
            f"team_delete must fire even on exception — got call order: {names}"
        )
        assert "team_create" in names

    def test_outcome_success_shape(self, tmp_path):
        tools = MockTeamTools(send_responses=["agenda: ship it"])
        with patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}):
            outcome = team_cycle_executor(
                clone_dir=tmp_path,
                project=_project(),
                run_row=_run_row(subject="my subject"),
                cycle_n=2,
                tools=tools,
            )
        assert outcome["status"] == "success"
        assert outcome["subject"] == "my subject"
        assert isinstance(outcome["commit_sha"], str)
        assert isinstance(outcome["minutes_memex_slug"], str)
        assert isinstance(outcome["participants"], list)
        # Exactly these 5 keys — no leftover abandonment fields leaking into a success outcome.
        assert set(outcome.keys()) == {
            "status",
            "subject",
            "commit_sha",
            "minutes_memex_slug",
            "participants",
        }

    def test_outcome_abandon_shape_when_participant_signals_ABANDON(self, tmp_path):
        tools = MockTeamTools(send_responses=["ABANDON: cannot reach consensus on scope"])
        with patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}):
            outcome = team_cycle_executor(
                clone_dir=tmp_path,
                project=_project(),
                run_row=_run_row(),
                cycle_n=1,
                tools=tools,
            )
        assert outcome["status"] == "abandoned"
        assert outcome["phase_reached"] in VALID_PHASES
        assert outcome["reason"] in VALID_REASONS
        assert "cannot reach consensus on scope" in outcome["detail"]
        assert isinstance(outcome["participants"], list)
        assert isinstance(outcome["artifacts"], list)
        # Exactly these 11 fields per internal/cycle/SKILL.md — no extras
        # (the 4 Phase 5b' optional fields may be None but the keys must exist;
        # success-only keys like commit_sha / minutes_memex_slug must NOT leak).
        assert set(outcome.keys()) == {
            "status",
            "subject",
            "participants",
            "phase_reached",
            "reason",
            "detail",
            "artifacts",
            "review_iteration_count",
            "unresolved_findings",
            "convergence_summary",
            "reviewer_attribution",
        }

    def test_team_create_passes_expert_roster_as_members(self, tmp_path):
        roster = ["pm-1", "software-architect-1", "backend-engineer-1"]
        tools = MockTeamTools()
        with patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}):
            team_cycle_executor(
                clone_dir=tmp_path,
                project=_project(roster=roster),
                run_row=_run_row(),
                cycle_n=1,
                tools=tools,
            )
        create_calls = [c for c in tools.calls if c[0] == "team_create"]
        assert len(create_calls) == 1
        assert create_calls[0][2]["members"] == roster

    def test_team_id_threaded_through_send_message_and_delete(self, tmp_path):
        tools = MockTeamTools()
        with patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}):
            team_cycle_executor(
                clone_dir=tmp_path,
                project=_project(),
                run_row=_run_row(),
                cycle_n=1,
                tools=tools,
            )
        # MockTeamTools returns f"team-{name}" so we can identify it.
        send_team_ids = [c[1][0] for c in tools.calls if c[0] == "send_message"]
        delete_team_ids = [c[1][0] for c in tools.calls if c[0] == "team_delete"]
        assert send_team_ids, "expected at least one send_message call"
        assert delete_team_ids, "expected exactly one team_delete call"
        # All send_message and team_delete invocations must use the SAME
        # team_id that team_create returned.
        ids = set(send_team_ids) | set(delete_team_ids)
        assert len(ids) == 1, f"team_id was not threaded consistently: {ids}"

    def test_minutes_slug_matches_run_id_and_cycle_n(self, tmp_path):
        tools = MockTeamTools()
        with patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}):
            outcome = team_cycle_executor(
                clone_dir=tmp_path,
                project=_project(),
                run_row=_run_row(run_id=42),
                cycle_n=7,
                tools=tools,
            )
        assert outcome["minutes_memex_slug"] == "kaizen:cycle:42-7"

    def test_abandoned_outcome_passes_run_py_allowlist_guard(self, tmp_path):
        """The abandoned outcome must pass scripts.run.orchestrate_run's
        VALID_PHASES/VALID_REASONS guards before any DB write.

        Mirrors the exact membership check in scripts/run.py lines ~280-291.
        """
        tools = MockTeamTools(send_responses=["ABANDON: out of scope for this cycle"])
        with patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}):
            outcome = team_cycle_executor(
                clone_dir=tmp_path,
                project=_project(),
                run_row=_run_row(),
                cycle_n=1,
                tools=tools,
            )
        phase_reached = outcome.get("phase_reached")
        reason = outcome.get("reason")
        if phase_reached not in VALID_PHASES:
            raise ValueError(
                f"cycle 1 outcome has invalid 'phase_reached'={phase_reached!r}; "
                f"valid values per migration 004: {sorted(VALID_PHASES)}"
            )
        if reason not in VALID_REASONS:
            raise ValueError(
                f"cycle 1 outcome has invalid 'reason'={reason!r}; "
                f"valid values per migration 004: {sorted(VALID_REASONS)}"
            )

    def test_team_delete_fires_when_response_signals_abandon(self, tmp_path):
        """Abandonment must still tear down the team — same invariant as
        exception path, just a different control flow."""
        tools = MockTeamTools(send_responses=["ABANDON: nope"])
        with patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}):
            team_cycle_executor(
                clone_dir=tmp_path,
                project=_project(),
                run_row=_run_row(),
                cycle_n=1,
                tools=tools,
            )
        names = [c[0] for c in tools.calls]
        assert names[-1] == "team_delete", (
            f"team_delete must fire on abandonment too — got call order: {names}"
        )

    def test_error_message_mentions_env_var(self, tmp_path):
        """The TeamToolsUnavailableError message must mention the env var so
        the operator knows exactly what to set."""
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(TeamToolsUnavailableError) as exc_info:
                team_cycle_executor(tmp_path, _project(), _run_row(), 1, tools=MockTeamTools())
            assert "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS" in str(exc_info.value)

    def test_signature_accepts_keyword_only_tools(self):
        """team_cycle_executor must accept the same positional args as
        scripts.cycle.execute_cycle, plus a keyword-only `tools` parameter.

        This preserves the orchestrator's executor-swap contract: positional
        callers (mode='subagent') continue to work; team-mode callers pass
        `tools=` as a kwarg.
        """
        import inspect

        from scripts.cycle import execute_cycle

        team_sig = inspect.signature(team_cycle_executor)
        subagent_sig = inspect.signature(execute_cycle)
        subagent_params = list(subagent_sig.parameters.keys())
        team_params = list(team_sig.parameters.keys())
        # All subagent positional params must appear (in order) at the start
        # of team_cycle_executor's signature.
        assert team_params[: len(subagent_params)] == subagent_params, (
            f"positional prefix mismatch: team={team_params} subagent={subagent_params}"
        )
        # The extra parameter must be keyword-only and named `tools`.
        extras = team_params[len(subagent_params) :]
        assert extras == ["tools"], f"unexpected extra params: {extras}"
        assert team_sig.parameters["tools"].kind == inspect.Parameter.KEYWORD_ONLY, (
            "`tools` must be keyword-only"
        )
