"""Tests for scripts/cycle.py — per-cycle DB row writes + stub executor."""
from __future__ import annotations

import pytest

from scripts.migrate import MIGRATIONS_DIR, apply_migrations
from scripts.cycle import (
    execute_cycle,
    get_cycle,
    list_cycles,
    record_cycle_abandoned,
    record_cycle_success,
)
from scripts.run import create_run
from scripts.project import create_project


@pytest.fixture
def db(tmp_path) -> str:
    db_path = str(tmp_path / "kaizen.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    return db_path


@pytest.fixture
def run_row(db) -> dict:
    project = create_project(
        db,
        git_url="https://github.com/owner/repo.git",
        name="repo",
        base_branch="main",
        test_command="pytest",
        read_paths=[],
        expert_roster=[],
        language="python",
    )
    return create_run(
        db,
        project_id=project["id"],
        branch="kaizen/test-2026-05-16-1200",
        cycles_requested=3,
        subject="test",
    )


def test_record_cycle_success(db, run_row):
    row = record_cycle_success(
        db_path=db,
        run_id=run_row["id"],
        cycle_n=1,
        subject="improve docs",
        commit_sha="abc123",
        minutes_memex_slug="kaizen:cycle:test-1",
        started_at="2026-05-16T12:00:00+00:00",
    )
    assert row["status"] == "success"
    assert row["cycle_n"] == 1
    assert row["commit_sha"] == "abc123"
    assert row["minutes_memex_slug"] == "kaizen:cycle:test-1"
    assert row["ended_at"]  # auto-filled


def test_record_cycle_abandoned(db, run_row):
    row = record_cycle_abandoned(
        db_path=db,
        run_id=run_row["id"],
        cycle_n=2,
        subject="reorg",
        started_at="2026-05-16T12:00:00+00:00",
    )
    assert row["status"] == "abandoned"
    assert row["cycle_n"] == 2
    assert row["commit_sha"] is None
    assert row["minutes_memex_slug"] is None


def test_get_cycle_returns_row_or_none(db, run_row):
    inserted = record_cycle_success(
        db_path=db,
        run_id=run_row["id"],
        cycle_n=1,
        subject="x",
        commit_sha="deadbeef",
        minutes_memex_slug=None,
        started_at="2026-05-16T12:00:00+00:00",
    )
    fetched = get_cycle(db, inserted["id"])
    assert fetched == inserted
    assert get_cycle(db, 99_999) is None


def test_list_cycles_orders_by_cycle_n(db, run_row):
    record_cycle_success(db, run_row["id"], 3, "c", "sha3", None,
                        "2026-05-16T12:00:00+00:00")
    record_cycle_abandoned(db, run_row["id"], 1, "a",
                           "2026-05-16T12:00:00+00:00")
    record_cycle_success(db, run_row["id"], 2, "b", "sha2", None,
                        "2026-05-16T12:00:00+00:00")
    rows = list_cycles(db, run_row["id"])
    assert [r["cycle_n"] for r in rows] == [1, 2, 3]
    assert [r["status"] for r in rows] == ["abandoned", "success", "success"]


def test_execute_cycle_raises_NotImplementedError(tmp_path):
    with pytest.raises(NotImplementedError) as exc_info:
        execute_cycle(tmp_path, {"name": "x"}, {"id": 1}, 1)
    assert "internal/cycle/SKILL.md" in str(exc_info.value)
    assert "cycle_executor" in str(exc_info.value)
