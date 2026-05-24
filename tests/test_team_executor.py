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
  - the Phase 1-5c orchestration dispatches in the right order, runs
    the DAG validator, mirrors CI at wave boundaries, selects disjoint
    reviewers, runs the fix loop, and commits a real SHA at the end
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

import scripts.team_executor as team_executor_mod
from scripts.abandonment import VALID_PHASES, VALID_REASONS
from scripts.team_executor import TeamToolsUnavailableError, team_cycle_executor

# ── MockTeamTools fixture ─────────────────────────────────────────────────


class MockTeamTools:
    """Records every tool call; returns scripted send_message responses keyed by phase.

    `scripted` is a dict whose keys are substrings of the outgoing message;
    the first key that's a substring of the message wins and its value is
    returned. Falls back to `default` (typically the literal "ack" / "NO ISSUES")
    when no key matches.

    `raise_on_send_call_n` (1-indexed) makes the Nth send_message raise a
    RuntimeError so tests can prove team_delete still fires.
    """

    def __init__(
        self,
        scripted: dict[str, str] | None = None,
        default: str = "NO ISSUES",
        raise_on_send_call_n: int | None = None,
    ):
        self.calls: list[tuple[str, tuple, dict]] = []
        self.scripted = dict(scripted or {})
        self.default = default
        self._raise_on_send_call_n = raise_on_send_call_n
        self._send_call_count = 0

    def team_create(self, name, members):
        self.calls.append(("team_create", (name,), {"members": list(members)}))
        return f"team-{name}"

    def send_message(self, team_id, to, message):
        self._send_call_count += 1
        self.calls.append(("send_message", (team_id, to), {"message": message[:120]}))
        if self._raise_on_send_call_n == self._send_call_count:
            raise RuntimeError(f"injected send_message failure on call {self._send_call_count}")
        for key, resp in self.scripted.items():
            if key in message:
                return resp
        return self.default

    def send_message_many(self, messages):
        """Batch wrapper for tests — record each as a send_message call so
        existing assertions ("recipient is in this list", "wave order is
        [A,B,C]") keep working unchanged. Each individual call also goes
        through the call counter so `raise_on_send_call_n` still works."""
        out = []
        for m in messages:
            out.append(self.send_message(m["team_id"], m["to"], m["message"]))
        return out

    def team_delete(self, team_id):
        self.calls.append(("team_delete", (team_id,), {}))


def _project(roster: list[str] | None = None) -> dict:
    return {
        "name": "test-project",
        "git_url": "https://example.invalid/test.git",
        "expert_roster": roster if roster is not None else ["pm-1", "backend-engineer-1"],
        "test_command": "pytest",
    }


def _run_row(run_id: int = 1, subject: str | None = "test subject") -> dict:
    return {"id": run_id, "subject": subject}


# Standard "happy path" scripted responses — one Action Item, no findings.
_DEFAULT_AGENDA = "Phase 1"
_DEFAULT_AI_JSON = (
    "Phase 3 close",
    'ok\n```json\n[{"id": "A", "touches": ["x.py"], "reads": [], '
    '"depends_on": [], "wave": 1, "owner": "backend-engineer-1"}]\n```',
)


def _happy_scripted() -> dict[str, str]:
    """Scripted responses that drive the executor through to commit."""
    return {
        "Phase 1": "do the work\nfix the bugs",
        "Phase 2": "I propose a small change",
        "Phase 3 open": "noted",
        "Phase 3 debate": "agreed",
        "Phase 3 close": _DEFAULT_AI_JSON[1],
        "Phase 4 wave": "applied the change",
        "Phase 5b'": "NO ISSUES",
    }


def _patch_phase5c(monkeypatch):
    """Stub commit_cycle + subprocess.run rev-parse so Phase 5c does not need a real repo."""

    def fake_commit_cycle(**kwargs):
        return None

    monkeypatch.setattr(team_executor_mod, "commit_cycle", fake_commit_cycle)

    class _FakeProc:
        def __init__(self, stdout):
            self.stdout = stdout
            self.returncode = 0

    def fake_run(cmd, **kwargs):
        return _FakeProc("deadbeefcafebabe1234567890abcdef12345678\n")

    monkeypatch.setattr(team_executor_mod.subprocess, "run", fake_run)


def _patch_ci_green(monkeypatch):
    """Stub run_ci_checks to always return green so Phase 4 wave boundaries pass."""
    calls: list = []

    def fake_run_ci_checks(clone_dir, test_command):
        calls.append((clone_dir, test_command))
        return True, {"tests": (True, "ok")}

    monkeypatch.setattr(team_executor_mod, "run_ci_checks", fake_run_ci_checks)
    return calls


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


# ── Preflight (preserved) ─────────────────────────────────────────────────


class TestTeamCycleExecutorPreflight:
    """The 4 preflight contracts — env, None tools, Protocol shape, signature."""

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
        """Env-var present but tools=None must still raise — no silent degrade."""
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
        msg = str(exc_info.value)
        assert "tools=None" in msg
        assert "TeamTools" in msg

    def test_raises_when_tools_missing_required_method(self, tmp_path):
        """Runtime Protocol check — reject malformed wrappers BEFORE team_create."""

        class PartialTools:
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
        msg = str(exc_info.value)
        assert "send_message" in msg
        assert "missing required method" in msg

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

    def test_error_message_mentions_env_var(self, tmp_path):
        """The TeamToolsUnavailableError message must name the env var."""
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(TeamToolsUnavailableError) as exc_info:
                team_cycle_executor(tmp_path, _project(), _run_row(), 1, tools=MockTeamTools())
            assert "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS" in str(exc_info.value)

    def test_signature_accepts_keyword_only_tools(self):
        """Same positional surface as execute_cycle plus keyword-only `tools`."""
        import inspect

        from scripts.cycle import execute_cycle

        team_sig = inspect.signature(team_cycle_executor)
        subagent_sig = inspect.signature(execute_cycle)
        subagent_params = list(subagent_sig.parameters.keys())
        team_params = list(team_sig.parameters.keys())
        assert team_params[: len(subagent_params)] == subagent_params, (
            f"positional prefix mismatch: team={team_params} subagent={subagent_params}"
        )
        extras = team_params[len(subagent_params) :]
        assert extras == ["tools"], f"unexpected extra params: {extras}"
        assert team_sig.parameters["tools"].kind == inspect.Parameter.KEYWORD_ONLY, (
            "`tools` must be keyword-only"
        )


# ── Outcome shape + lifecycle (Phase 1-5c integrated) ─────────────────────


class TestTeamCycleExecutorLifecycle:
    """The high-level lifecycle invariants survive the multi-phase orchestration."""

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
        names = [c[0] for c in tools.calls]
        assert names[-1] == "team_delete", (
            f"team_delete must fire even on exception — got call order: {names}"
        )
        assert "team_create" in names

    def test_team_delete_fires_when_response_signals_abandon(self, tmp_path):
        """Abandonment via ABANDON: response must still tear down the team."""
        tools = MockTeamTools(scripted={"Phase 1": "ABANDON: nope"})
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

    def test_team_create_passes_expert_roster_as_members(self, tmp_path):
        roster = ["pm-1", "software-architect-1", "backend-engineer-1"]
        # Abandon at agenda to keep the test minimal.
        tools = MockTeamTools(scripted={"Phase 1": "ABANDON: skip"})
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
        tools = MockTeamTools(scripted={"Phase 1": "ABANDON: skip"})
        with patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}):
            team_cycle_executor(
                clone_dir=tmp_path,
                project=_project(),
                run_row=_run_row(),
                cycle_n=1,
                tools=tools,
            )
        send_team_ids = [c[1][0] for c in tools.calls if c[0] == "send_message"]
        delete_team_ids = [c[1][0] for c in tools.calls if c[0] == "team_delete"]
        assert send_team_ids, "expected at least one send_message call"
        assert delete_team_ids, "expected exactly one team_delete call"
        ids = set(send_team_ids) | set(delete_team_ids)
        assert len(ids) == 1, f"team_id was not threaded consistently: {ids}"

    def test_minutes_slug_matches_run_id_and_cycle_n(self, tmp_path, monkeypatch):
        # Happy path through all six phases so we exercise the success outcome.
        roster = ["pm-1", "backend-engineer-1", "security-engineer-1"]
        _patch_ci_green(monkeypatch)
        _patch_phase5c(monkeypatch)
        tools = MockTeamTools(scripted=_happy_scripted())
        with patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}):
            outcome = team_cycle_executor(
                clone_dir=tmp_path,
                project=_project(roster=roster),
                run_row=_run_row(run_id=42),
                cycle_n=7,
                tools=tools,
            )
        assert outcome["minutes_memex_slug"] == "kaizen:cycle:42-7"


# ── Phase 1 — Agenda ──────────────────────────────────────────────────────


class TestPhase1Agenda:
    def test_phase_1_calls_send_message_to_pm(self, tmp_path):
        """First send_message recipient is roster[0] (the PM)."""
        roster = ["pm-1", "backend-engineer-1", "security-engineer-1"]
        tools = MockTeamTools(scripted={"Phase 1": "ABANDON: stop"})
        with patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}):
            team_cycle_executor(
                clone_dir=tmp_path,
                project=_project(roster=roster),
                run_row=_run_row(),
                cycle_n=1,
                tools=tools,
            )
        send_calls = [c for c in tools.calls if c[0] == "send_message"]
        assert send_calls, "expected at least one send_message"
        first_recipient = send_calls[0][1][1]
        assert first_recipient == "pm-1"

    def test_phase_1_abandon_returns_abandon_outcome(self, tmp_path):
        """PM ABANDON at agenda → phase_reached='agenda', reason='other'."""
        tools = MockTeamTools(scripted={"Phase 1": "ABANDON: nothing useful"})
        with patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}):
            outcome = team_cycle_executor(
                clone_dir=tmp_path,
                project=_project(),
                run_row=_run_row(),
                cycle_n=1,
                tools=tools,
            )
        assert outcome["status"] == "abandoned"
        assert outcome["phase_reached"] == "agenda"
        assert outcome["reason"] == "other"
        assert "nothing useful" in outcome["detail"]

    def test_phase_1_empty_response_abandons_with_clear_detail(self, tmp_path):
        """An empty PM response → agenda/other with a parseable detail message."""
        tools = MockTeamTools(scripted={"Phase 1": "   \n  \n"})
        with patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}):
            outcome = team_cycle_executor(
                clone_dir=tmp_path,
                project=_project(),
                run_row=_run_row(),
                cycle_n=1,
                tools=tools,
            )
        assert outcome["status"] == "abandoned"
        assert outcome["phase_reached"] == "agenda"
        assert "no agenda items" in outcome["detail"]


# ── Phase 2 — Pre-analysis ────────────────────────────────────────────────


class TestPhase2Preanalysis:
    def test_phase_2_dispatches_to_each_non_pm_roster_member(self, tmp_path):
        """For roster [a,b,c,d], Phase 2 send_messages are to b, c, d (a is pm)."""
        roster = ["pm-1", "be-1", "se-1", "arch-1"]
        # Abandon at Phase 3 close to stop the cycle quickly but after Phase 2 ran.
        tools = MockTeamTools(
            scripted={
                "Phase 1": "do x",
                "Phase 3 close": "ABANDON: skip",
            }
        )
        with patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}):
            team_cycle_executor(
                clone_dir=tmp_path,
                project=_project(roster=roster),
                run_row=_run_row(),
                cycle_n=1,
                tools=tools,
            )
        # Filter to Phase 2 send_messages only — identified by the message
        # snippet prefix the executor uses.
        phase_2_recipients = [
            c[1][1] for c in tools.calls if c[0] == "send_message" and "Phase 2" in c[2]["message"]
        ]
        assert phase_2_recipients == ["be-1", "se-1", "arch-1"], (
            f"expected dispatch to non-PM roster in order; got {phase_2_recipients}"
        )


# ── Phase 3 — Synthesis ───────────────────────────────────────────────────


class TestPhase3Synthesis:
    def test_phase_3_synthesis_validates_dag_cycle_abandons(self, tmp_path):
        """Action Items with a CYCLE → abandon phase=meeting reason=no_consensus."""
        cyclic_json = (
            "ok\n```json\n["
            '{"id": "A", "touches": ["a.py"], "reads": [], "depends_on": ["B"], '
            '"wave": 1, "owner": "backend-engineer-1"}, '
            '{"id": "B", "touches": ["b.py"], "reads": [], "depends_on": ["A"], '
            '"wave": 1, "owner": "backend-engineer-1"}'
            "]\n```"
        )
        tools = MockTeamTools(
            scripted={
                "Phase 1": "do x",
                "Phase 2": "proposal",
                "Phase 3 close": cyclic_json,
            }
        )
        with patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}):
            outcome = team_cycle_executor(
                clone_dir=tmp_path,
                project=_project(roster=["pm-1", "backend-engineer-1"]),
                run_row=_run_row(),
                cycle_n=1,
                tools=tools,
            )
        assert outcome["status"] == "abandoned"
        assert outcome["phase_reached"] == "meeting"
        assert outcome["reason"] == "no_consensus"
        assert "cycle" in outcome["detail"].lower() or "depends_on" in outcome["detail"].lower()

    def test_phase_3_no_json_block_abandons(self, tmp_path):
        """PM close that lacks a ```json``` block → no_consensus abandonment."""
        tools = MockTeamTools(
            scripted={
                "Phase 1": "do x",
                "Phase 2": "proposal",
                "Phase 3 close": "Sorry I forgot the JSON block.",
            }
        )
        with patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}):
            outcome = team_cycle_executor(
                clone_dir=tmp_path,
                project=_project(roster=["pm-1", "backend-engineer-1"]),
                run_row=_run_row(),
                cycle_n=1,
                tools=tools,
            )
        assert outcome["status"] == "abandoned"
        assert outcome["phase_reached"] == "meeting"
        assert outcome["reason"] == "no_consensus"
        assert "json" in outcome["detail"].lower()


# ── Phase 4 — Waves ───────────────────────────────────────────────────────


class TestPhase4Waves:
    def _two_wave_json(self) -> str:
        return (
            "ok\n```json\n["
            '{"id": "A", "touches": ["a.py"], "reads": [], "depends_on": [], '
            '"wave": 1, "owner": "be-1"}, '
            '{"id": "B", "touches": ["b.py"], "reads": [], "depends_on": [], '
            '"wave": 1, "owner": "se-1"}, '
            '{"id": "C", "touches": ["c.py"], "reads": [], "depends_on": ["A"], '
            '"wave": 2, "owner": "be-1"}'
            "]\n```"
        )

    def test_phase_4_dispatches_in_wave_order(self, tmp_path, monkeypatch):
        """Given AIs [A wave 1, B wave 1, C wave 2], send_message order is A, B, then C."""
        _patch_ci_green(monkeypatch)
        _patch_phase5c(monkeypatch)
        tools = MockTeamTools(
            scripted={
                "Phase 1": "do x",
                "Phase 2": "proposal",
                "Phase 3 close": self._two_wave_json(),
                "Phase 4 wave 1 — implement Action Item A": "did A",
                "Phase 4 wave 1 — implement Action Item B": "did B",
                "Phase 4 wave 2 — implement Action Item C": "did C",
                "Phase 5b'": "NO ISSUES",
            }
        )
        roster = ["pm-1", "be-1", "se-1", "security-engineer-1", "architect-1"]
        with patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}):
            team_cycle_executor(
                clone_dir=tmp_path,
                project=_project(roster=roster),
                run_row=_run_row(),
                cycle_n=1,
                tools=tools,
            )
        phase_4_items = []
        for c in tools.calls:
            if c[0] != "send_message":
                continue
            msg = c[2]["message"]
            if "Phase 4 wave" not in msg:
                continue
            # Extract the Action Item id from the message snippet.
            for ai_id in ("A", "B", "C"):
                token = f"Action Item {ai_id}"
                if token in msg:
                    phase_4_items.append(ai_id)
                    break
        assert phase_4_items == ["A", "B", "C"], f"wave order broken: {phase_4_items}"

    def test_phase_4_runs_ci_at_wave_boundary(self, tmp_path, monkeypatch):
        """ci_runner.run_ci_checks fires once per wave."""
        ci_calls = _patch_ci_green(monkeypatch)
        _patch_phase5c(monkeypatch)
        tools = MockTeamTools(
            scripted={
                "Phase 1": "do x",
                "Phase 2": "proposal",
                "Phase 3 close": self._two_wave_json(),
                "Phase 4 wave": "applied",
                "Phase 5b'": "NO ISSUES",
            }
        )
        roster = ["pm-1", "be-1", "se-1", "security-engineer-1", "architect-1"]
        with patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}):
            team_cycle_executor(
                clone_dir=tmp_path,
                project=_project(roster=roster),
                run_row=_run_row(),
                cycle_n=1,
                tools=tools,
            )
        # Two waves → two CI invocations.
        assert len(ci_calls) == 2, f"expected 2 CI runs (one per wave); got {len(ci_calls)}"

    def test_phase_4_ci_failure_abandons_with_tests_unrecoverable(self, tmp_path, monkeypatch):
        """A red CI at a wave boundary abandons phase=test reason=tests_unrecoverable."""

        def fake_ci(clone_dir, test_command):
            return False, {"tests": (False, "boom")}

        monkeypatch.setattr(team_executor_mod, "run_ci_checks", fake_ci)
        _patch_phase5c(monkeypatch)
        tools = MockTeamTools(
            scripted={
                "Phase 1": "do x",
                "Phase 2": "proposal",
                "Phase 3 close": self._two_wave_json(),
                "Phase 4 wave": "applied",
            }
        )
        roster = ["pm-1", "be-1", "se-1", "security-engineer-1", "architect-1"]
        with patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}):
            outcome = team_cycle_executor(
                clone_dir=tmp_path,
                project=_project(roster=roster),
                run_row=_run_row(),
                cycle_n=1,
                tools=tools,
            )
        assert outcome["status"] == "abandoned"
        assert outcome["phase_reached"] == "test"
        assert outcome["reason"] == "tests_unrecoverable"
        assert "tests" in outcome["detail"]


# ── Phase 5b' — Reviewers ─────────────────────────────────────────────────


class TestPhase5BPrimeReviewers:
    def _six_roster_one_ai_json(self) -> str:
        return (
            "ok\n```json\n["
            '{"id": "A", "touches": ["a.py"], "reads": [], "depends_on": [], '
            '"wave": 1, "owner": "be-1"}'
            "]\n```"
        )

    def test_phase_5b_prime_selects_disjoint_reviewers(self, tmp_path, monkeypatch):
        """Roster of 6 with 1 implementer → reviewers chosen are disjoint."""
        _patch_ci_green(monkeypatch)
        _patch_phase5c(monkeypatch)
        # Roster of 6 ensures at least 3 disjoint candidates after removing `be-1`.
        roster = [
            "pm-1",
            "be-1",
            "security-engineer-1",
            "software-architect-1",
            "prompt-engineer-1",
            "sdet-1",
        ]
        tools = MockTeamTools(
            scripted={
                "Phase 1": "do x",
                "Phase 2": "proposal",
                "Phase 3 close": self._six_roster_one_ai_json(),
                "Phase 4 wave": "applied",
                "Phase 5b'": "NO ISSUES",
            }
        )
        with patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}):
            team_cycle_executor(
                clone_dir=tmp_path,
                project=_project(roster=roster),
                run_row=_run_row(),
                cycle_n=1,
                tools=tools,
            )
        # The reviewer briefs go to the disjoint set; the implementer (be-1)
        # must NEVER receive a Phase 5b' message.
        reviewer_recipients = {
            c[1][1]
            for c in tools.calls
            if c[0] == "send_message" and "Phase 5b'" in c[2]["message"]
        }
        assert reviewer_recipients, "expected Phase 5b' reviewer dispatches"
        assert "be-1" not in reviewer_recipients, (
            f"implementer leaked into reviewer set: {reviewer_recipients}"
        )
        # We requested min(3, disjoint_pool) reviewers; pool is 5 → expect 3.
        assert len(reviewer_recipients) == 3, (
            f"expected 3 disjoint reviewers; got {sorted(reviewer_recipients)}"
        )

    def test_phase_5b_prime_fix_loop_exhaustion_returns_review_unrecoverable(
        self, tmp_path, monkeypatch
    ):
        """Reviewer always returns a blocker → after 5 iterations, review_unrecoverable."""
        _patch_ci_green(monkeypatch)
        _patch_phase5c(monkeypatch)
        # A persistent blocker that the reviewer surfaces every iteration.
        persistent_blocker = "[blocker] a.py:1 — wrong"
        roster = [
            "pm-1",
            "be-1",
            "security-engineer-1",
            "software-architect-1",
            "prompt-engineer-1",
            "sdet-1",
        ]
        tools = MockTeamTools(
            scripted={
                "Phase 1": "do x",
                "Phase 2": "proposal",
                "Phase 3 close": self._six_roster_one_ai_json(),
                "Phase 4 wave": "applied",
                # Reviewer brief lands → return a blocker every time.
                "Phase 5b' iteration": persistent_blocker,
                # Fix brief lands → reviewer says ok, but next iter still flags.
                "Phase 5b' fix": "tried",
            }
        )
        with patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}):
            outcome = team_cycle_executor(
                clone_dir=tmp_path,
                project=_project(roster=roster),
                run_row=_run_row(),
                cycle_n=1,
                tools=tools,
            )
        assert outcome["status"] == "abandoned"
        assert outcome["phase_reached"] == "review"
        assert outcome["reason"] == "review_unrecoverable"
        assert outcome["review_iteration_count"] == 5
        assert outcome["unresolved_findings"], "expected unresolved findings list"
        assert outcome["convergence_summary"] is not None
        # The convergence summary mentions iteration count or persistence.
        assert "iterations" in outcome["convergence_summary"].lower()


# ── Phase 5c — Commit ─────────────────────────────────────────────────────


class TestPhase5CCommit:
    def test_phase_5c_uses_commit_cycle_real_sha(self, tmp_path, monkeypatch):
        """Happy path returns a real-looking SHA (not '(skeleton)')."""
        _patch_ci_green(monkeypatch)

        commit_calls: list = []

        def fake_commit_cycle(**kwargs):
            commit_calls.append(kwargs)

        monkeypatch.setattr(team_executor_mod, "commit_cycle", fake_commit_cycle)

        class _FakeProc:
            stdout = "1234567890abcdef\n"
            returncode = 0

        def fake_run(cmd, **kwargs):
            return _FakeProc()

        monkeypatch.setattr(team_executor_mod.subprocess, "run", fake_run)

        roster = ["pm-1", "be-1", "security-engineer-1", "software-architect-1"]
        ai_json = (
            "ok\n```json\n["
            '{"id": "A", "touches": ["a.py"], "reads": [], "depends_on": [], '
            '"wave": 1, "owner": "be-1"}'
            "]\n```"
        )
        tools = MockTeamTools(
            scripted={
                "Phase 1": "do x",
                "Phase 2": "proposal",
                "Phase 3 close": ai_json,
                "Phase 4 wave": "applied",
                "Phase 5b'": "NO ISSUES",
            }
        )
        with patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}):
            outcome = team_cycle_executor(
                clone_dir=tmp_path,
                project=_project(roster=roster),
                run_row=_run_row(),
                cycle_n=1,
                tools=tools,
            )
        assert outcome["status"] == "success"
        assert outcome["commit_sha"] == "1234567890abcdef"
        assert outcome["commit_sha"] != "(skeleton)"
        assert len(commit_calls) == 1, "commit_cycle must be invoked exactly once"


# ── Outcome shapes (preserved + adapted) ──────────────────────────────────


class TestOutcomeShapes:
    def test_outcome_success_shape(self, tmp_path, monkeypatch):
        _patch_ci_green(monkeypatch)
        _patch_phase5c(monkeypatch)
        roster = ["pm-1", "be-1", "security-engineer-1", "software-architect-1"]
        ai_json = (
            "ok\n```json\n["
            '{"id": "A", "touches": ["a.py"], "reads": [], "depends_on": [], '
            '"wave": 1, "owner": "be-1"}'
            "]\n```"
        )
        tools = MockTeamTools(
            scripted={
                "Phase 1": "do x",
                "Phase 2": "proposal",
                "Phase 3 close": ai_json,
                "Phase 4 wave": "applied",
                "Phase 5b'": "NO ISSUES",
            }
        )
        with patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}):
            outcome = team_cycle_executor(
                clone_dir=tmp_path,
                project=_project(roster=roster),
                run_row=_run_row(subject="my subject"),
                cycle_n=2,
                tools=tools,
            )
        assert outcome["status"] == "success"
        assert outcome["subject"] == "my subject"
        assert isinstance(outcome["commit_sha"], str)
        assert isinstance(outcome["minutes_memex_slug"], str)
        assert isinstance(outcome["participants"], list)
        assert set(outcome.keys()) == {
            "status",
            "subject",
            "commit_sha",
            "minutes_memex_slug",
            "participants",
        }

    def test_outcome_abandon_shape_when_participant_signals_ABANDON(self, tmp_path):
        tools = MockTeamTools(scripted={"Phase 1": "ABANDON: cannot reach consensus on scope"})
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

    def test_abandoned_outcome_passes_run_py_allowlist_guard(self, tmp_path):
        """Mirror the orchestrator's VALID_PHASES/VALID_REASONS guard."""
        tools = MockTeamTools(scripted={"Phase 1": "ABANDON: out of scope"})
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


# ── New: team_delete fires even when Phase 5 helper raises ────────────────


class TestPhase5HelperFailureCleanup:
    def test_team_delete_fires_when_run_ci_checks_raises(self, tmp_path, monkeypatch):
        """If run_ci_checks raises, the team is still torn down in finally."""

        def boom(clone_dir, test_command):
            raise RuntimeError("ci runner blew up")

        monkeypatch.setattr(team_executor_mod, "run_ci_checks", boom)
        ai_json = (
            "ok\n```json\n["
            '{"id": "A", "touches": ["a.py"], "reads": [], "depends_on": [], '
            '"wave": 1, "owner": "be-1"}'
            "]\n```"
        )
        roster = ["pm-1", "be-1", "security-engineer-1"]
        tools = MockTeamTools(
            scripted={
                "Phase 1": "do x",
                "Phase 2": "proposal",
                "Phase 3 close": ai_json,
                "Phase 4 wave": "applied",
            }
        )
        with (
            patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}),
            pytest.raises(RuntimeError, match="ci runner blew up"),
        ):
            team_cycle_executor(
                clone_dir=tmp_path,
                project=_project(roster=roster),
                run_row=_run_row(),
                cycle_n=1,
                tools=tools,
            )
        names = [c[0] for c in tools.calls]
        assert names[-1] == "team_delete", (
            f"team_delete must fire even when Phase 4 CI raises — got: {names}"
        )


# ── Major-fix regressions (Park's reviewer findings) ──────────────────────


class TestPhase5BPrimeMajorFixes:
    """Cover the 4 majors Marcus Holbrook surfaced in independent review."""

    def _ai_json_with_owner(self, owner: str, file: str = "foo.py") -> str:
        return (
            "ok\n```json\n["
            f'{{"id": "A", "touches": ["{file}"], "reads": [], "depends_on": [], '
            f'"wave": 1, "owner": "{owner}"}}'
            "]\n```"
        )

    def test_fix_brief_routes_to_implementer_not_reviewer(self, tmp_path, monkeypatch):
        """Major 1: Phase 5b' fix briefs must land at the AI's `owner` (the
        implementer), NEVER the reviewer who surfaced the finding.

        Setup: roster=[pm, be, security, arch]; one AI owned by `be-1`
        touching `foo.py`; reviewer `security-engineer-1` returns a blocker
        on `foo.py:10`. Assert the resulting fix-brief send_message landed
        at `be-1`, not at `security-engineer-1`.
        """
        _patch_ci_green(monkeypatch)
        _patch_phase5c(monkeypatch)
        roster = [
            "pm-1",
            "be-1",
            "security-engineer-1",
            "software-architect-1",
            "prompt-engineer-1",
        ]
        # First reviewer round: blocker. Then reviewers go quiet on round 2
        # (after the fix lands) so the loop exits clean.
        # Use a stateful response generator via a list cursor on scripted.
        # We simply script the same blocker for the reviewer brief and 'NO
        # ISSUES' for the rest; the existing scripted-dict-first-match
        # contract handles ordering.
        # Trick: route by message substring. Reviewer brief contains
        # "Phase 5b' iteration"; PM acceptance contains "PM acceptance";
        # fix brief contains "Phase 5b' fix". After round 1's fix, round 2
        # uses the SAME reviewer key so we need the reviewer to flip to
        # NO ISSUES. We do that via a CallableScripted subclass.

        round_state = {"n": 0}

        class CallableMock(MockTeamTools):
            def send_message(self_inner, team_id, to, message):
                # Replicate parent's call accounting first.
                self_inner._send_call_count += 1
                self_inner.calls.append(("send_message", (team_id, to), {"message": message[:160]}))
                if "Phase 5b' iteration" in message:
                    round_state["n"] += 1
                    if round_state["n"] == 1:
                        return "[blocker] foo.py:10 — bad"
                    return "NO ISSUES"
                if "Phase 5b' fix" in message:
                    return "fixed it"
                if "PM acceptance" in message:
                    return "REJECT — keep iterating"
                for key, resp in self_inner.scripted.items():
                    if key in message:
                        return resp
                return self_inner.default

        tools = CallableMock(
            scripted={
                "Phase 1": "do x",
                "Phase 2": "proposal",
                "Phase 3 close": self._ai_json_with_owner("be-1", file="foo.py"),
                "Phase 4 wave": "applied",
            }
        )
        with patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}):
            outcome = team_cycle_executor(
                clone_dir=tmp_path,
                project=_project(roster=roster),
                run_row=_run_row(),
                cycle_n=1,
                tools=tools,
            )
        assert outcome["status"] == "success", f"unexpected: {outcome}"
        fix_calls = [
            c for c in tools.calls if c[0] == "send_message" and "Phase 5b' fix" in c[2]["message"]
        ]
        assert len(fix_calls) >= 1, "expected at least one Phase 5b' fix dispatch"
        fix_recipients = [c[1][1] for c in fix_calls]
        # The CRITICAL invariant — fixes go to the implementer, NOT the
        # reviewer who flagged the finding.
        assert all(r == "be-1" for r in fix_recipients), (
            f"fix briefs must land at the implementer (be-1), not the reviewer; "
            f"got recipients={fix_recipients}"
        )
        assert "security-engineer-1" not in fix_recipients, (
            "regression: fix brief routed to the reviewer (Major 1 bug returned)"
        )

    def test_pm_acceptance_exits_fix_loop_cleanly(self, tmp_path, monkeypatch):
        """Major 2: PM ACCEPT response → loop exits clean even with majors
        outstanding (success outcome, no review_unrecoverable abandonment).
        """
        _patch_ci_green(monkeypatch)
        _patch_phase5c(monkeypatch)
        roster = [
            "pm-1",
            "be-1",
            "security-engineer-1",
            "software-architect-1",
            "prompt-engineer-1",
        ]

        class PMAcceptMock(MockTeamTools):
            def send_message(self_inner, team_id, to, message):
                self_inner._send_call_count += 1
                self_inner.calls.append(("send_message", (team_id, to), {"message": message[:160]}))
                if "Phase 5b' iteration" in message:
                    # Always surface a major finding so the fix loop must
                    # consult the PM.
                    return "[major] bar.py:5 — style nit but blocking"
                if "PM acceptance" in message:
                    return "ACCEPT — minor issues are acceptable for this round"
                if "Phase 5b' fix" in message:
                    return "fixed"
                for key, resp in self_inner.scripted.items():
                    if key in message:
                        return resp
                return self_inner.default

        tools = PMAcceptMock(
            scripted={
                "Phase 1": "do x",
                "Phase 2": "proposal",
                "Phase 3 close": self._ai_json_with_owner("be-1", file="bar.py"),
                "Phase 4 wave": "applied",
            }
        )
        with patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}):
            outcome = team_cycle_executor(
                clone_dir=tmp_path,
                project=_project(roster=roster),
                run_row=_run_row(),
                cycle_n=1,
                tools=tools,
            )
        assert outcome["status"] == "success", (
            f"PM ACCEPT must exit fix loop cleanly; got {outcome}"
        )
        # The PM-acceptance brief MUST have been sent (proof we plumbed
        # `pm_accepts_remaining` through).
        pm_accept_calls = [
            c for c in tools.calls if c[0] == "send_message" and "PM acceptance" in c[2]["message"]
        ]
        assert len(pm_accept_calls) >= 1, "PM acceptance brief was never sent"
        # And the fix loop must NOT have run 5 iterations — PM accepted on
        # iteration 1, so there's at most one reviewer round.
        reviewer_rounds = [
            c
            for c in tools.calls
            if c[0] == "send_message" and "Phase 5b' iteration" in c[2]["message"]
        ]
        # 3 reviewers x 1 iteration = 3 reviewer messages.
        assert len(reviewer_rounds) == 3, (
            f"expected exactly 1 reviewer round (3 recipients); got {len(reviewer_rounds)}"
        )

    def test_reviewer_brief_carries_prior_findings_on_iter2(self, tmp_path, monkeypatch):
        """Major 3: iteration 2's reviewer brief contains a 'Previously
        unresolved' section; iteration 1's brief does not.
        """
        _patch_ci_green(monkeypatch)
        _patch_phase5c(monkeypatch)
        roster = [
            "pm-1",
            "be-1",
            "security-engineer-1",
            "software-architect-1",
            "prompt-engineer-1",
        ]

        captured_iterations: list[str] = []

        class CaptureMock(MockTeamTools):
            def send_message(self_inner, team_id, to, message):
                self_inner._send_call_count += 1
                # Record the FULL message (not truncated) so we can inspect
                # the prior-findings block.
                self_inner.calls.append(("send_message", (team_id, to), {"message": message}))
                if "Phase 5b' iteration" in message:
                    captured_iterations.append(message)
                    # Round 1: blocker; round 2: NO ISSUES to exit cleanly.
                    if len(captured_iterations) <= 3:  # 3 reviewers in round 1
                        return "[blocker] baz.py:1 — broken"
                    return "NO ISSUES"
                if "Phase 5b' fix" in message:
                    return "fixed"
                if "PM acceptance" in message:
                    return "REJECT — try once more"
                for key, resp in self_inner.scripted.items():
                    if key in message:
                        return resp
                return self_inner.default

        tools = CaptureMock(
            scripted={
                "Phase 1": "do x",
                "Phase 2": "proposal",
                "Phase 3 close": self._ai_json_with_owner("be-1", file="baz.py"),
                "Phase 4 wave": "applied",
            }
        )
        with patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}):
            outcome = team_cycle_executor(
                clone_dir=tmp_path,
                project=_project(roster=roster),
                run_row=_run_row(),
                cycle_n=1,
                tools=tools,
            )
        assert outcome["status"] == "success", f"unexpected: {outcome}"
        # We expect 2 iterations x 3 reviewers = 6 reviewer messages.
        assert len(captured_iterations) >= 6, (
            f"expected at least 2 iterations x 3 reviewers; got {len(captured_iterations)}"
        )
        # The first 3 messages (iteration 1) must NOT contain the carry
        # forward block. The next 3 (iteration 2) MUST contain it.
        iter1_msgs = captured_iterations[:3]
        iter2_msgs = captured_iterations[3:6]
        for msg in iter1_msgs:
            assert "Previously unresolved" not in msg, (
                "iteration 1 reviewer brief must NOT carry prior findings"
            )
        for msg in iter2_msgs:
            assert "Previously unresolved" in msg, (
                "iteration 2 reviewer brief MUST carry prior findings "
                f"(Major 3 regression); got: {msg[:300]!r}"
            )
            # The carried-forward finding should mention its severity + file_line.
            assert "blocker" in msg.lower()
            assert "baz.py:1" in msg

    def test_blocking_severities_imported_from_fix_loop(self):
        """Major 4: the executor uses fix_loop's blocking-severity set, not a
        local re-definition (avoids drift across modules).
        """
        import scripts.team_executor as te_mod
        from scripts.fix_loop import _BLOCKING_SEVERITIES as canonical

        # The executor must import the canonical frozenset by reference.
        assert te_mod._BLOCKING_SEVERITIES is canonical, (
            "scripts.team_executor must reuse scripts.fix_loop._BLOCKING_SEVERITIES "
            "(Major 4 — avoid duplicate constant drift)"
        )
        # And the local `_BLOCKING` constant must NOT exist anymore.
        assert not hasattr(te_mod, "_BLOCKING"), (
            "team_executor still defines a local _BLOCKING constant — remove it "
            "and use _BLOCKING_SEVERITIES instead (Major 4)"
        )


# ── _find_owner_for_finding unowned-file warning ──────────────────────────


def test_find_owner_for_finding_logs_warning_when_file_unowned(caplog):
    """Item 1: when a finding's file maps to no owner, fall back to PM and
    emit a logging.warning naming BOTH the unowned file and the responsible
    reviewer so an operator can audit the routing decision after the fact.
    """
    import logging

    from scripts.fix_loop import Finding
    from scripts.team_executor import _find_owner_for_finding

    f = Finding(
        finding_id="R1-9",
        reviewer="security-engineer-7",
        severity="blocker",
        finding="unowned cross-cutting issue",
        file_line="scripts/orphan_file.py:42",
    )
    file_to_owner = {"scripts/owned.py": "backend-engineer-1"}

    with caplog.at_level(logging.WARNING, logger="scripts.team_executor"):
        owner = _find_owner_for_finding(f, file_to_owner, pm="pm-1")

    assert owner == "pm-1"
    # The warning text must name BOTH the reviewer who flagged the finding
    # AND the unowned file path so the routing decision is auditable.
    warnings = [rec.getMessage() for rec in caplog.records if rec.levelno == logging.WARNING]
    assert any("security-engineer-7" in w for w in warnings), (
        f"warning must name the reviewer; got: {warnings}"
    )
    assert any("scripts/orphan_file.py" in w for w in warnings), (
        f"warning must name the unowned file; got: {warnings}"
    )


def test_find_owner_for_finding_does_not_warn_when_file_owned(caplog):
    """Item 1 negative: when the file IS owned, no warning fires (we don't
    want log noise on the happy path).
    """
    import logging

    from scripts.fix_loop import Finding
    from scripts.team_executor import _find_owner_for_finding

    f = Finding(
        finding_id="R1-1",
        reviewer="security-engineer-1",
        severity="blocker",
        finding="x",
        file_line="scripts/owned.py:1",
    )
    file_to_owner = {"scripts/owned.py": "backend-engineer-1"}

    with caplog.at_level(logging.WARNING, logger="scripts.team_executor"):
        owner = _find_owner_for_finding(f, file_to_owner, pm="pm-1")

    assert owner == "backend-engineer-1"
    assert not any(rec.levelno == logging.WARNING for rec in caplog.records), (
        "no warning should fire when the file IS owned"
    )
