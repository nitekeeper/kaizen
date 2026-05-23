"""Tests for scripts/run.py — CRUD + orchestrator integration."""

from __future__ import annotations

from pathlib import Path

import pytest

import scripts.run as run_mod
from scripts.migrate import MIGRATIONS_DIR, apply_migrations
from scripts.project import create_project
from scripts.run import (
    create_run,
    finalize_run,
    get_run,
    list_runs,
    orchestrate_run,
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
