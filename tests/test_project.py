"""Tests for scripts/project.py — CRUD module + minimal CLI smoke."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.db import get_connection
from scripts.migrate import MIGRATIONS_DIR, apply_migrations
from scripts.project import (
    create_project,
    delete_project,
    get_project,
    get_project_by_url,
    list_projects,
    update_project,
)


@pytest.fixture
def db(tmp_path) -> str:
    db_path = str(tmp_path / "kaizen.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    return db_path


def _make(db, **overrides) -> dict:
    base = {
        "git_url": "https://github.com/owner/repo.git",
        "name": "repo",
        "base_branch": "main",
        "test_command": "pytest -v --tb=short",
        "read_paths": ["scripts/*.py", "tests/*.py"],
        "expert_roster": ["agent-systems-architect-1", "backend-engineer-1"],
        "language": "python",
        "notes": None,
    }
    base.update(overrides)
    return create_project(db, **base)


# ── create + get roundtrip ────────────────────────────────────────────────


def test_create_and_get_roundtrip(db):
    created = _make(db)
    assert created["id"] >= 1
    fetched = get_project(db, created["id"])
    assert fetched is not None
    assert fetched["git_url"] == created["git_url"]
    assert fetched["name"] == "repo"
    assert fetched["base_branch"] == "main"
    assert fetched["language"] == "python"
    assert fetched["registered_at"]  # ISO timestamp present
    assert fetched["last_run_at"] is None


def test_lists_are_returned_as_python_lists_not_json_strings(db):
    created = _make(db)
    assert isinstance(created["read_paths"], list)
    assert created["read_paths"] == ["scripts/*.py", "tests/*.py"]
    assert isinstance(created["expert_roster"], list)
    assert "agent-systems-architect-1" in created["expert_roster"]


def test_lists_stored_as_json_strings_in_db(db):
    _make(db)
    with get_connection(db) as conn:
        row = conn.execute("SELECT read_paths, expert_roster FROM projects LIMIT 1").fetchone()
    # Raw DB values are JSON-encoded strings.
    assert isinstance(row[0], str)
    assert json.loads(row[0]) == ["scripts/*.py", "tests/*.py"]
    assert isinstance(row[1], str)
    assert json.loads(row[1])[0] == "agent-systems-architect-1"


# ── get_project_by_url ─────────────────────────────────────────────────────


def test_get_project_by_url_finds_the_right_row(db):
    _make(db, git_url="https://github.com/a/one.git", name="one")
    target = _make(db, git_url="https://github.com/a/two.git", name="two")
    _make(db, git_url="https://github.com/a/three.git", name="three")

    found = get_project_by_url(db, "https://github.com/a/two.git")
    assert found is not None
    assert found["id"] == target["id"]
    assert found["name"] == "two"


def test_get_project_by_url_returns_none_when_missing(db):
    assert get_project_by_url(db, "https://nope/none.git") is None


# ── list_projects ──────────────────────────────────────────────────────────


def test_list_projects_returns_all_in_id_order(db):
    a = _make(db, git_url="https://github.com/x/a.git", name="a")
    b = _make(db, git_url="https://github.com/x/b.git", name="b")
    c = _make(db, git_url="https://github.com/x/c.git", name="c")
    rows = list_projects(db)
    assert [r["id"] for r in rows] == [a["id"], b["id"], c["id"]]
    assert all(isinstance(r["read_paths"], list) for r in rows)


def test_list_projects_empty(db):
    assert list_projects(db) == []


# ── update_project ─────────────────────────────────────────────────────────


def test_update_project_mutates_scalar_fields(db):
    created = _make(db)
    updated = update_project(db, created["id"], name="renamed", notes="now with notes")
    assert updated["name"] == "renamed"
    assert updated["notes"] == "now with notes"
    # Untouched fields preserved.
    assert updated["git_url"] == created["git_url"]


def test_update_project_round_trips_list_fields(db):
    created = _make(db)
    new_paths = ["src/**/*.py", "tests/**/*.py", "docs/**/*"]
    updated = update_project(db, created["id"], read_paths=new_paths)
    assert updated["read_paths"] == new_paths
    refetched = get_project(db, created["id"])
    assert refetched["read_paths"] == new_paths


def test_update_project_ignores_unknown_fields(db):
    created = _make(db)
    # `id` is not in the updatable set; should be quietly dropped.
    updated = update_project(db, created["id"], id=999, name="ok")
    assert updated["id"] == created["id"]
    assert updated["name"] == "ok"


# ── delete_project ─────────────────────────────────────────────────────────


def test_delete_project_removes_row(db):
    created = _make(db)
    assert delete_project(db, created["id"]) is True
    assert get_project(db, created["id"]) is None


def test_delete_project_returns_false_when_missing(db):
    assert delete_project(db, 99_999) is False


# ── UNIQUE constraint ─────────────────────────────────────────────────────


def test_create_duplicate_git_url_raises_integrity_error(db):
    _make(db, git_url="https://github.com/dupe/dupe.git", name="one")
    with pytest.raises(sqlite3.IntegrityError):
        _make(db, git_url="https://github.com/dupe/dupe.git", name="two")


# ── update_project column-name validation (M6) ────────────────────────────


def test_update_project_rejects_invalid_column_name_when_allowlist_bypassed(db, monkeypatch):
    """Regex guard raises ValueError even when _UPDATABLE is extended with a bad key."""
    from scripts import project

    created = _make(db)
    evil_key = "name; DROP TABLE projects--"
    monkeypatch.setattr(project, "_UPDATABLE", project._UPDATABLE | {evil_key})
    with pytest.raises(ValueError, match="Invalid column name"):
        update_project(db, created["id"], **{evil_key: "evil"})


def test_update_project_silently_drops_unknown_keys(db):
    """Keys not in _UPDATABLE are silently ignored; project is returned unchanged."""
    created = _make(db)
    result = update_project(db, created["id"], not_a_real_column="x")
    assert result["id"] == created["id"]
    assert result["name"] == created["name"]
    assert result["git_url"] == created["git_url"]


# ── CLI smoke test ─────────────────────────────────────────────────────────


def test_cli_list_against_empty_db_returns_empty_array(tmp_path):
    db_path = tmp_path / ".ai" / "memex.db"
    db_path.parent.mkdir(parents=True)
    apply_migrations(str(db_path), MIGRATIONS_DIR)
    project_root = Path(__file__).resolve().parents[1]
    script = project_root / "scripts" / "project.py"
    proc = subprocess.run(
        [sys.executable, str(script), "list"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={
            **__import__("os").environ,
            "PYTHONPATH": str(project_root),
        },
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == []
