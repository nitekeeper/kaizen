"""Tests for scripts/team_executor.py — team agent mode cycle executor."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from scripts.team_executor import TeamToolsUnavailableError, team_cycle_executor

# ── _check_team_tools_available ───────────────────────────────────────────


class TestCheckTeamToolsAvailable:
    """The guard function raises when the env var is absent or falsy."""

    @pytest.mark.parametrize("value", ["1", "true", "True", "TRUE", "yes", "YES"])
    def test_does_not_raise_when_env_truthy(self, value):
        with patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": value}):
            # Import here so we get the real function after patching env.
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
    """team_cycle_executor raises TeamToolsUnavailableError when env absent,
    and NotImplementedError when env is present (real execution needs tools)."""

    def test_raises_unavailable_when_env_absent(self, tmp_path):
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"}
        with patch.dict(os.environ, env, clear=True), pytest.raises(TeamToolsUnavailableError):
            team_cycle_executor(
                clone_dir=tmp_path,
                project={"name": "test"},
                run_row={"id": 1, "subject": None},
                cycle_n=1,
            )

    def test_raises_not_implemented_when_env_set(self, tmp_path):
        """With the env var set, the guard passes but the stub raises
        NotImplementedError — real execution requires live tool calls."""
        with (
            patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}),
            pytest.raises(NotImplementedError),
        ):
            team_cycle_executor(
                clone_dir=tmp_path,
                project={"name": "test"},
                run_row={"id": 1, "subject": None},
                cycle_n=1,
            )

    def test_signature_matches_execute_cycle(self):
        """team_cycle_executor must accept the same positional args as
        scripts.cycle.execute_cycle so the orchestrator can swap executors."""
        import inspect

        from scripts.cycle import execute_cycle

        team_sig = inspect.signature(team_cycle_executor)
        subagent_sig = inspect.signature(execute_cycle)
        assert list(team_sig.parameters.keys()) == list(subagent_sig.parameters.keys()), (
            "team_cycle_executor signature must match execute_cycle: "
            f"team={list(team_sig.parameters.keys())} "
            f"subagent={list(subagent_sig.parameters.keys())}"
        )

    def test_error_message_mentions_env_var(self, tmp_path):
        """The TeamToolsUnavailableError message must mention the env var so
        the operator knows exactly what to set."""
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(TeamToolsUnavailableError) as exc_info:
                team_cycle_executor(tmp_path, {}, {}, 1)
            assert "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS" in str(exc_info.value)
