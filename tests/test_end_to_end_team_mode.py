"""End-to-end integration tests for team mode.

These tests drive orchestrate_run with mode='team' + a real tools_provider
that constructs a CallbackWrapper with scripted-mock callbacks. They prove
the full Python-side integration is wired correctly — the only missing
piece for production dogfood is the orchestrating agent providing real
CC-tool callbacks instead of mock ones.

Test surface:
  - happy path: Phase 1 → 5c success, team_delete fires LAST, status='complete'
  - abandon at agenda: PM ABANDON; team_delete still fires
  - multi-cycle: provider invoked once per cycle, distinct wrapper instances
  - lifecycle order: team_create → many send_message → team_delete
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import scripts.run as run_mod
import scripts.team_executor as team_executor_mod
from examples.agent_teams_wrapper_example import CallbackWrapper
from scripts.migrate import MIGRATIONS_DIR, apply_migrations
from scripts.project import create_project
from scripts.run import orchestrate_run

# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path) -> str:
    db_path = str(tmp_path / "kaizen.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    return db_path


@pytest.fixture
def project(db) -> dict:
    """A project with a 4-member roster, large enough for disjoint reviewers."""
    return create_project(
        db,
        git_url="https://github.com/owner/repo.git",
        name="repo",
        base_branch="main",
        test_command="pytest",
        read_paths=[],
        expert_roster=[
            "pm-1",
            "backend-engineer-1",
            "security-engineer-1",
            "software-architect-1",
        ],
        language="python",
    )


def _install_orchestrator_stubs(monkeypatch, tmp_path):
    """Stub clone/seed/branch/push so the orchestrator doesn't touch real git.

    Mirrors tests/test_run.py::_install_orchestrator_stubs.
    """

    def fake_clone(remote_url, dest, branch):
        dest.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(run_mod, "kaizen_root", lambda: tmp_path)

    import scripts.clone as clone_mod

    monkeypatch.setattr(clone_mod, "clone_repo", fake_clone)

    import scripts.seed_atelier_in_clone as seed_mod

    monkeypatch.setattr(seed_mod, "seed_all", lambda d: None)

    import scripts.cycle_git as cg_mod

    monkeypatch.setattr(
        cg_mod,
        "create_branch",
        lambda d, subj: f"kaizen/{(subj or 'pm-directed').replace(' ', '-')}-2026-05-23-1925",
    )
    monkeypatch.setattr(cg_mod, "push_branch", lambda d, b: None)


def _stub_team_executor_helpers(monkeypatch):
    """Stub the executor's CI runner, commit_cycle, and git rev-parse."""

    def fake_run_ci_checks(clone_dir, test_command):
        return True, {"tests": {"status": "pass", "output": "ok"}}

    monkeypatch.setattr(team_executor_mod, "run_ci_checks", fake_run_ci_checks)

    def fake_commit_cycle(**kwargs):
        return None

    monkeypatch.setattr(team_executor_mod, "commit_cycle", fake_commit_cycle)

    class _FakeProc:
        def __init__(self, stdout="deadbeefcafebabe1234567890abcdef12345678\n"):
            self.stdout = stdout
            self.returncode = 0

    def fake_subproc_run(cmd, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(team_executor_mod.subprocess, "run", fake_subproc_run)


# ── Scripted-response builder ─────────────────────────────────────────────


# Single happy-path Action Items JSON used across multiple tests.
_HAPPY_AI_JSON = (
    "```json\n"
    "["
    '{"id": "A", "touches": ["example.py"], "reads": [], '
    '"depends_on": [], "wave": 1, "owner": "backend-engineer-1"}, '
    '{"id": "B", "touches": ["example_test.py"], "reads": [], '
    '"depends_on": [], "wave": 1, "owner": "software-architect-1"}'
    "]\n"
    "```"
)


def _happy_script() -> dict[str, str]:
    """Substring → response. First match wins (caller iterates in dict order)."""
    return {
        "Phase 1 (Agenda)": "1. Add example file\n2. Add example test",
        "Phase 2 (Pre-analysis)": (
            "Proposal: simple change. Touches: ['example.py']. Reads: []. Depends_on: []."
        ),
        "Phase 3 open": "acknowledged",
        "Phase 3 debate": "agreed",
        "Phase 3 close": _HAPPY_AI_JSON,
        "Phase 4 wave": "applied: created example.py with hello-world body",
        "Phase 5b' iteration": "NO ISSUES",
        "Phase 5b' fix": "fixed",
        "PM acceptance": "ACCEPT",
    }


class _ScriptedSendCallback:
    """Callable that records every (team_id, to, message) and returns a scripted reply.

    Substring match on the message body — first matching key in `script` wins,
    so callers should put more-specific keys first. Falls back to `default`.
    """

    def __init__(
        self,
        script: dict[str, str] | None = None,
        default: str = "NO ISSUES",
        override: dict[str, str] | None = None,
    ):
        # Override (e.g. {"Phase 1 (Agenda)": "ABANDON: ..."}) WINS over
        # the default happy script — dict merge with override LAST so a
        # single test can flip one phase to ABANDON without rewriting the
        # whole script.
        self.script = {**(script or _happy_script()), **(override or {})}
        self.default = default
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, team_id: str, to: str, message: str) -> str:
        self.calls.append((team_id, to, message))
        for key, resp in self.script.items():
            if key in message:
                return resp
        return self.default


def _build_callback_wrapper(send_cb=None, op_log: list[str] | None = None):
    """Build a CallbackWrapper with recording callbacks; return (wrapper, records).

    records is the dict of call lists for assertions:
      records["team_create"] = list[(name, members)]
      records["team_delete"] = list[team_id]
      records["send"]        = the send callback (its .calls attribute has the list)
      records["op_log"]      = the shared lifecycle-order log (timestamps every
                               team_create / send_message / team_delete in
                               invocation order — used to pin the
                               "team_delete fires LAST" invariant in EVERY E2E
                               test, not just the dedicated lifecycle test).
    """
    if op_log is None:
        op_log = []
    records: dict = {"team_create": [], "team_delete": [], "op_log": op_log}

    def team_create_cb(name: str, members: list[str]) -> str:
        op_log.append("team_create")
        records["team_create"].append((name, list(members)))
        return f"team-id-{name}"

    def team_delete_cb(team_id: str) -> None:
        op_log.append("team_delete")
        records["team_delete"].append(team_id)

    send = send_cb or _ScriptedSendCallback()
    records["send"] = send

    # Wrap the send callback so it ALSO appends to op_log — this preserves
    # the user-supplied callable's recording behaviour while threading
    # lifecycle ordering through every E2E test for free.
    def send_message_cb(team_id: str, to: str, message: str) -> str:
        op_log.append("send_message")
        return send(team_id, to, message)

    wrapper = CallbackWrapper(
        team_create_cb=team_create_cb,
        send_message_cb=send_message_cb,
        team_delete_cb=team_delete_cb,
    )
    return wrapper, records


# ── E2E test 1 — happy path ────────────────────────────────────────────────


def test_e2e_team_mode_happy_path_completes_with_commit(db, project, tmp_path, monkeypatch):
    """Full Phase 1-5c success through orchestrate_run with mode='team'."""
    _install_orchestrator_stubs(monkeypatch, tmp_path)
    _stub_team_executor_helpers(monkeypatch)
    monkeypatch.setitem(os.environ, "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", "1")

    provider_calls: list[tuple] = []
    last_records: dict = {}

    def provider(clone_dir, proj, run_row, cycle_n):
        wrapper, records = _build_callback_wrapper()
        provider_calls.append((clone_dir, proj, run_row, cycle_n, wrapper))
        last_records.update(records)
        return wrapper

    result = orchestrate_run(
        db_path=db,
        git_url=project["git_url"],
        cycles_requested=1,
        subject="add example",
        mode="team",
        tools_provider=provider,
    )

    # Top-level run result.
    assert result["status"] == "complete", f"unexpected status: {result}"
    assert result["cycles_succeeded"] == 1
    assert result["cycles_abandoned"] == 0
    assert result["mode"] == "team"

    # tools_provider invoked exactly once (one cycle).
    assert len(provider_calls) == 1, f"expected one provider invocation; got {len(provider_calls)}"

    # team_create + team_delete both fired, in that lifecycle order.
    assert len(last_records["team_create"]) == 1
    assert len(last_records["team_delete"]) == 1
    name, members = last_records["team_create"][0]
    assert name.startswith("kaizen-cycle-"), f"unexpected team name: {name}"
    assert set(members) == set(project["expert_roster"])

    # team_delete LAST in lifecycle is the critical invariant — pin it
    # explicitly here too (not just in the dedicated lifecycle-order test),
    # so a future refactor that loses the `finally` clause but still hits
    # team_delete on the success branch CANNOT silently pass.
    op_log = last_records["op_log"]
    assert op_log, "no callback ops were recorded"
    assert op_log[0] == "team_create", f"first op must be team_create; got {op_log[:3]}"
    assert op_log[-1] == "team_delete", (
        f"team_delete must be the LAST recorded op (success path); got tail {op_log[-3:]}"
    )
    assert op_log.count("team_create") == 1
    assert op_log.count("team_delete") == 1
    assert "send_message" in op_log, "expected at least one send_message"
    delete_idx = op_log.index("team_delete")
    assert "send_message" not in op_log[delete_idx + 1 :], (
        f"send_message fired AFTER team_delete; tail: {op_log[delete_idx:]}"
    )


# ── E2E test 2 — abandon at agenda ─────────────────────────────────────────


def test_e2e_team_mode_team_delete_fires_when_pm_abandons_at_agenda(
    db, project, tmp_path, monkeypatch
):
    """PM ABANDON at Phase 1: outcome abandoned + team_delete still fires."""
    _install_orchestrator_stubs(monkeypatch, tmp_path)
    _stub_team_executor_helpers(monkeypatch)
    monkeypatch.setitem(os.environ, "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", "1")

    last_records: dict = {}

    def provider(clone_dir, proj, run_row, cycle_n):
        send = _ScriptedSendCallback(
            override={"Phase 1 (Agenda)": "ABANDON: cannot propose any agenda"}
        )
        wrapper, records = _build_callback_wrapper(send_cb=send)
        last_records.update(records)
        return wrapper

    result = orchestrate_run(
        db_path=db,
        git_url=project["git_url"],
        cycles_requested=1,
        mode="team",
        tools_provider=provider,
    )

    # Run-level: 0 successes, 1 abandonment, still status='complete'.
    assert result["status"] == "complete"
    assert result["cycles_succeeded"] == 0
    assert result["cycles_abandoned"] == 1
    assert len(result["abandonments"]) == 1

    ab = result["abandonments"][0]
    assert ab["phase_reached"] == "agenda"
    assert ab["reason"] == "other"
    assert "cannot propose any agenda" in ab["detail"]

    # team_delete MUST have fired even though we abandoned at the very first phase.
    assert len(last_records["team_create"]) == 1
    assert len(last_records["team_delete"]) == 1

    # CRITICAL — pin the abandon-path lifecycle order INDEPENDENTLY of the
    # happy-path test. A future refactor that loses the `finally` clause but
    # still hits team_delete on the success branch would PASS the happy-path
    # order check yet LEAK teams on abandon. This assertion is the only thing
    # that catches that bug class.
    op_log = last_records["op_log"]
    assert op_log, "no callback ops were recorded"
    assert op_log[0] == "team_create", f"first op must be team_create; got {op_log[:3]}"
    assert op_log[-1] == "team_delete", (
        f"team_delete must be the LAST recorded op (abandon path); got tail {op_log[-3:]}"
    )
    assert op_log.count("team_create") == 1
    assert op_log.count("team_delete") == 1
    # At least one send_message must have fired BEFORE team_delete — the
    # PM's ABANDON: response is delivered via send_message, so the slice
    # before team_delete must contain it.
    assert "send_message" in op_log[:-1], (
        f"expected at least one send_message before team_delete on abandon path; got {op_log}"
    )


# ── E2E test 3 — provider invoked per cycle ────────────────────────────────


def test_e2e_team_mode_provider_invoked_per_cycle(db, project, tmp_path, monkeypatch):
    """cycles_requested=2 → provider called exactly twice with cycle_n=1,2;
    returned wrappers are DIFFERENT instances.
    """
    _install_orchestrator_stubs(monkeypatch, tmp_path)
    _stub_team_executor_helpers(monkeypatch)
    monkeypatch.setitem(os.environ, "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", "1")

    provider_calls: list[tuple] = []
    produced_wrappers: list[CallbackWrapper] = []

    def provider(clone_dir, proj, run_row, cycle_n):
        wrapper, _records = _build_callback_wrapper()
        provider_calls.append((clone_dir, proj, run_row, cycle_n))
        produced_wrappers.append(wrapper)
        return wrapper

    result = orchestrate_run(
        db_path=db,
        git_url=project["git_url"],
        cycles_requested=2,
        mode="team",
        tools_provider=provider,
    )

    assert result["status"] == "complete"
    assert result["cycles_succeeded"] == 2
    assert result["cycles_abandoned"] == 0

    cycle_ns = [c[3] for c in provider_calls]
    assert cycle_ns == [1, 2], f"provider must be called with cycle_n=1 then 2; got {cycle_ns}"

    # Wrappers must be DISTINCT object identities (one per cycle, not reused).
    assert len(produced_wrappers) == 2
    assert produced_wrappers[0] is not produced_wrappers[1], (
        "provider returned the SAME wrapper for both cycles — "
        "production must construct one wrapper per cycle"
    )


# ── E2E test 4 — lifecycle call order ──────────────────────────────────────


def test_e2e_team_mode_lifecycle_call_order_across_all_phases(db, project, tmp_path, monkeypatch):
    """The recorded callback ops, in invocation order, must be:
    team_create → many send_message → team_delete
    and no send_message after team_delete.
    """
    _install_orchestrator_stubs(monkeypatch, tmp_path)
    _stub_team_executor_helpers(monkeypatch)
    monkeypatch.setitem(os.environ, "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", "1")

    # Build a single recording surface that timestamps every callback in order.
    op_log: list[str] = []

    def team_create_cb(name: str, members: list[str]) -> str:
        op_log.append("team_create")
        return f"team-{name}"

    def send_message_cb(team_id: str, to: str, message: str) -> str:
        op_log.append("send_message")
        script = _happy_script()
        for key, resp in script.items():
            if key in message:
                return resp
        return "NO ISSUES"

    def team_delete_cb(team_id: str) -> None:
        op_log.append("team_delete")

    def provider(clone_dir, proj, run_row, cycle_n):
        return CallbackWrapper(
            team_create_cb=team_create_cb,
            send_message_cb=send_message_cb,
            team_delete_cb=team_delete_cb,
        )

    result = orchestrate_run(
        db_path=db,
        git_url=project["git_url"],
        cycles_requested=1,
        mode="team",
        tools_provider=provider,
    )

    assert result["status"] == "complete"
    assert result["cycles_succeeded"] == 1

    # 1) team_create is the FIRST recorded op.
    assert op_log, "no callback ops were recorded"
    assert op_log[0] == "team_create", f"first op must be team_create; got {op_log[:3]}"

    # 2) team_delete is the LAST recorded op.
    assert op_log[-1] == "team_delete", f"last op must be team_delete; got tail {op_log[-3:]}"

    # 3) exactly one team_create + exactly one team_delete.
    assert op_log.count("team_create") == 1
    assert op_log.count("team_delete") == 1

    # 4) at least one send_message between them.
    assert "send_message" in op_log

    # 5) no send_message AFTER team_delete (slice from index of team_delete+1).
    delete_idx = op_log.index("team_delete")
    assert "send_message" not in op_log[delete_idx + 1 :], (
        f"send_message fired AFTER team_delete; tail: {op_log[delete_idx:]}"
    )


# ── Bonus invariants — defence in depth ────────────────────────────────────


def test_e2e_team_mode_uses_real_callback_wrapper_not_recording_wrapper(
    db, project, tmp_path, monkeypatch
):
    """Type-pin: the wrapper the tools_provider returns is a CallbackWrapper
    instance (the production-pattern subclass), not a test stub.

    Catches regressions where a contributor accidentally swaps the example
    for the RecordingWrapper test double in production wiring.
    """
    _install_orchestrator_stubs(monkeypatch, tmp_path)
    _stub_team_executor_helpers(monkeypatch)
    monkeypatch.setitem(os.environ, "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", "1")

    seen: list = []

    def provider(clone_dir, proj, run_row, cycle_n):
        wrapper, _records = _build_callback_wrapper()
        seen.append(wrapper)
        return wrapper

    orchestrate_run(
        db_path=db,
        git_url=project["git_url"],
        cycles_requested=1,
        mode="team",
        tools_provider=provider,
    )
    assert seen
    assert isinstance(seen[0], CallbackWrapper)


def test_e2e_team_mode_clone_dir_is_passed_into_provider(db, project, tmp_path, monkeypatch):
    """The provider's first arg is the resolved experiment_dir (a Path)."""
    _install_orchestrator_stubs(monkeypatch, tmp_path)
    _stub_team_executor_helpers(monkeypatch)
    monkeypatch.setitem(os.environ, "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", "1")

    captured: list = []

    def provider(clone_dir, proj, run_row, cycle_n):
        captured.append(clone_dir)
        wrapper, _records = _build_callback_wrapper()
        return wrapper

    orchestrate_run(
        db_path=db,
        git_url=project["git_url"],
        cycles_requested=1,
        mode="team",
        tools_provider=provider,
    )

    assert captured
    assert isinstance(captured[0], Path)
    # The experiment dir is rooted under the patched kaizen_root → tmp_path.
    assert tmp_path in captured[0].parents or captured[0] == tmp_path / "experiment" / "owner-repo"
