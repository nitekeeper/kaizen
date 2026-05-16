"""Tests for scripts/abandonment.py — report format + memex + DB."""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

import scripts.abandonment as ab_mod
from scripts.abandonment import (
    capture_to_memex,
    format_report,
    process_abandonment,
    record_abandonment,
)
from scripts.cycle import record_cycle_abandoned
from scripts.migrate import MIGRATIONS_DIR, apply_migrations
from scripts.project import create_project
from scripts.run import create_run


@pytest.fixture
def db(tmp_path) -> str:
    db_path = str(tmp_path / "kaizen.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    return db_path


@pytest.fixture
def run_and_cycle(db) -> dict:
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
    run = create_run(
        db, project_id=project["id"], branch="kaizen/x-2026-05-16-1200",
        cycles_requested=1, subject="x",
    )
    cycle = record_cycle_abandoned(
        db, run_id=run["id"], cycle_n=1, subject="x",
        started_at="2026-05-16T12:00:00+00:00",
    )
    return {"project": project, "run": run, "cycle": cycle}


# ── format_report ──────────────────────────────────────────────────────────

def test_format_report_includes_all_required_fields():
    md = format_report(
        project_name="owner-repo",
        git_url="https://github.com/owner/repo.git",
        run_id=7,
        cycle_n=3,
        subject="improve error messages",
        participants=["pm", "backend-engineer-1", "ai-safety-researcher"],
        phase_reached="meeting",
        reason="no_consensus",
        detail="Agents could not agree on a unified error format.",
        artifacts=["kaizen:cycle:7-3-draft", "kaizen:cycle:7-3-logs"],
    )
    # Frontmatter
    assert "---\n" in md
    assert "id: kaizen:abandonment:7-cycle-3" in md
    assert "title: Cycle 3 abandoned — no_consensus" in md
    assert "type: abandonment-report" in md
    assert "project: owner-repo" in md
    assert "status: draft" in md
    # Body — every required field
    assert "Cycle: 3" in md
    assert "Date:" in md and "UTC" in md
    assert "Subject: improve error messages" in md
    assert "Participants: pm, backend-engineer-1, ai-safety-researcher" in md
    assert "Phase reached: meeting" in md
    assert "Reason for abandonment: no_consensus" in md
    assert "Detail: Agents could not agree on a unified error format." in md
    assert "Artifacts:" in md
    assert "kaizen:cycle:7-3-draft" in md


def test_format_report_handles_missing_subject_and_empty_lists():
    md = format_report(
        project_name="o-r", git_url="u", run_id=1, cycle_n=1,
        subject=None, participants=[], phase_reached="agenda",
        reason="other", detail="d", artifacts=[],
    )
    assert "Subject: PM-directed" in md
    assert "Participants: (none recorded)" in md
    assert "Artifacts: (none)" in md


# ── capture_to_memex ───────────────────────────────────────────────────────

def test_capture_to_memex_skipped_when_not_on_path(monkeypatch, capsys):
    monkeypatch.setattr(ab_mod.shutil, "which", lambda name: None)

    def fail_subprocess(*a, **kw):
        raise AssertionError("subprocess.run should not be called")
    monkeypatch.setattr(ab_mod.subprocess, "run", fail_subprocess)

    slug = capture_to_memex("kaizen:abandonment:1-cycle-1", "# body\n")
    assert slug == "kaizen:abandonment:1-cycle-1"
    err = capsys.readouterr().err
    assert "memex" in err and "PATH" in err


def test_capture_to_memex_handles_subprocess_failure(monkeypatch, capsys):
    monkeypatch.setattr(ab_mod.shutil, "which", lambda name: "/fake/memex")

    def fake_run(*a, **kw):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")
    monkeypatch.setattr(ab_mod.subprocess, "run", fake_run)

    slug = capture_to_memex("kaizen:abandonment:1-cycle-1", "# body\n")
    assert slug == "kaizen:abandonment:1-cycle-1"
    err = capsys.readouterr().err
    assert "exited 1" in err
    assert "boom" in err


def test_capture_to_memex_succeeds_silently(monkeypatch, capsys):
    monkeypatch.setattr(ab_mod.shutil, "which", lambda name: "/fake/memex")

    def fake_run(*a, **kw):
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(ab_mod.subprocess, "run", fake_run)

    slug = capture_to_memex("k:a:1-cycle-1", "# body\n")
    assert slug == "k:a:1-cycle-1"
    assert capsys.readouterr().err == ""


# ── record_abandonment ────────────────────────────────────────────────────

def test_record_abandonment_inserts_row(db, run_and_cycle):
    row = record_abandonment(
        db_path=db,
        cycle_id=run_and_cycle["cycle"]["id"],
        phase_reached="meeting",
        reason="no_consensus",
        detail="agents could not agree",
        report_memex_slug="kaizen:abandonment:1-cycle-1",
    )
    assert row["cycle_id"] == run_and_cycle["cycle"]["id"]
    assert row["phase_reached"] == "meeting"
    assert row["reason"] == "no_consensus"
    assert row["detail"] == "agents could not agree"
    assert row["report_memex_slug"] == "kaizen:abandonment:1-cycle-1"
    assert row["created_at"]


# ── process_abandonment full flow ──────────────────────────────────────────

def test_process_abandonment_full_flow(db, run_and_cycle, monkeypatch):
    # Force memex to be unavailable so the test doesn't shell out.
    monkeypatch.setattr(ab_mod.shutil, "which", lambda name: None)

    row = process_abandonment(
        db_path=db,
        project=run_and_cycle["project"],
        run_id=run_and_cycle["run"]["id"],
        cycle_id=run_and_cycle["cycle"]["id"],
        cycle_n=1,
        subject="x",
        participants=["pm"],
        phase_reached="meeting",
        reason="no_consensus",
        detail="d",
        artifacts=[],
    )
    expected_slug = f"kaizen:abandonment:{run_and_cycle['run']['id']}-cycle-1"
    assert row["report_memex_slug"] == expected_slug
    assert row["reason"] == "no_consensus"
    assert row["cycle_id"] == run_and_cycle["cycle"]["id"]
