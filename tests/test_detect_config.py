"""Tests for scripts/detect_config.py — language, test command, read paths."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.detect_config import (
    STANDING_ROSTER,
    default_expert_roster,
    detect_all,
    detect_language,
    detect_read_paths,
    detect_test_command,
)


# ── Fixture builders ────────────────────────────────────────────────────────

def _make_python_repo(root: Path, marker: str = "pyproject.toml") -> Path:
    (root / marker).write_text("# python project\n", encoding="utf-8")
    return root


def _make_javascript_repo(root: Path, test_script: str | None = "jest --coverage") -> Path:
    data: dict = {"name": "demo"}
    if test_script is not None:
        data["scripts"] = {"test": test_script}
    (root / "package.json").write_text(json.dumps(data), encoding="utf-8")
    return root


def _make_rust_repo(root: Path) -> Path:
    (root / "Cargo.toml").write_text("[package]\nname = \"demo\"\n", encoding="utf-8")
    return root


def _make_go_repo(root: Path) -> Path:
    (root / "go.mod").write_text("module demo\n", encoding="utf-8")
    return root


# ── Language detection ─────────────────────────────────────────────────────

def test_detect_language_python_pyproject(tmp_path):
    _make_python_repo(tmp_path, "pyproject.toml")
    assert detect_language(tmp_path) == "python"


def test_detect_language_python_pytest_ini(tmp_path):
    _make_python_repo(tmp_path, "pytest.ini")
    assert detect_language(tmp_path) == "python"


def test_detect_language_python_setup_py(tmp_path):
    _make_python_repo(tmp_path, "setup.py")
    assert detect_language(tmp_path) == "python"


def test_detect_language_javascript_with_test_script(tmp_path):
    _make_javascript_repo(tmp_path, "jest --coverage")
    assert detect_language(tmp_path) == "javascript"


def test_detect_language_javascript_missing_scripts_test_is_unknown(tmp_path):
    # package.json without scripts.test → not javascript per spec.
    _make_javascript_repo(tmp_path, test_script=None)
    assert detect_language(tmp_path) == "unknown"


def test_detect_language_rust(tmp_path):
    _make_rust_repo(tmp_path)
    assert detect_language(tmp_path) == "rust"


def test_detect_language_go(tmp_path):
    _make_go_repo(tmp_path)
    assert detect_language(tmp_path) == "go"


def test_detect_language_unknown(tmp_path):
    assert detect_language(tmp_path) == "unknown"


# ── Test command detection ─────────────────────────────────────────────────

def test_detect_test_command_python(tmp_path):
    assert detect_test_command(tmp_path, "python") == "pytest -v --tb=short"


def test_detect_test_command_javascript_uses_package_script(tmp_path):
    _make_javascript_repo(tmp_path, "jest --coverage")
    assert detect_test_command(tmp_path, "javascript") == "jest --coverage"


def test_detect_test_command_javascript_fallback_to_npm_test(tmp_path):
    # No package.json at all → fallback string used.
    assert detect_test_command(tmp_path, "javascript") == "npm test"


def test_detect_test_command_javascript_fallback_when_scripts_missing(tmp_path):
    _make_javascript_repo(tmp_path, test_script=None)
    assert detect_test_command(tmp_path, "javascript") == "npm test"


def test_detect_test_command_rust(tmp_path):
    assert detect_test_command(tmp_path, "rust") == "cargo test"


def test_detect_test_command_go(tmp_path):
    assert detect_test_command(tmp_path, "go") == "go test ./..."


def test_detect_test_command_unknown_returns_none(tmp_path):
    assert detect_test_command(tmp_path, "unknown") is None


# ── Read paths ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "language,expected",
    [
        ("python", ["scripts/*.py", "tests/*.py", "skills/*/SKILL.md", "CLAUDE.md", "README.md"]),
        ("javascript", ["src/**/*.{js,ts}", "test/**/*", "README.md"]),
        ("rust", ["src/**/*.rs", "tests/**/*", "Cargo.toml", "README.md"]),
        ("go", ["**/*.go", "go.mod", "README.md"]),
        ("unknown", []),
    ],
)
def test_detect_read_paths(tmp_path, language, expected):
    assert detect_read_paths(tmp_path, language) == expected


# ── Expert roster ──────────────────────────────────────────────────────────

def test_default_expert_roster_python_has_standing_plus_python_specialists():
    roster = default_expert_roster("python")
    for r in STANDING_ROSTER:
        assert r in roster
    assert "backend-engineer-1" in roster
    assert "data-engineer-1" in roster


def test_default_expert_roster_unknown_is_just_standing_six():
    assert default_expert_roster("unknown") == list(STANDING_ROSTER)
    assert len(default_expert_roster("unknown")) == 6


def test_default_expert_roster_javascript_specialists():
    roster = default_expert_roster("javascript")
    assert "frontend-engineer-1" in roster
    assert "fullstack-engineer-1" in roster


def test_default_expert_roster_rust_specialists():
    roster = default_expert_roster("rust")
    assert "systems-architect-1" in roster
    assert "backend-engineer-1" in roster


def test_default_expert_roster_go_specialists():
    roster = default_expert_roster("go")
    assert "backend-engineer-1" in roster
    assert "software-architect-1" in roster


# ── detect_all aggregator ──────────────────────────────────────────────────

def test_detect_all_python(tmp_path):
    _make_python_repo(tmp_path)
    out = detect_all(tmp_path)
    assert out["language"] == "python"
    assert out["test_command"] == "pytest -v --tb=short"
    assert out["read_paths"] == ["scripts/*.py", "tests/*.py", "skills/*/SKILL.md", "CLAUDE.md", "README.md"]
    assert "backend-engineer-1" in out["expert_roster"]


def test_detect_all_javascript(tmp_path):
    _make_javascript_repo(tmp_path, "jest --coverage")
    out = detect_all(tmp_path)
    assert out["language"] == "javascript"
    assert out["test_command"] == "jest --coverage"
    assert out["read_paths"] == ["src/**/*.{js,ts}", "test/**/*", "README.md"]
    assert "frontend-engineer-1" in out["expert_roster"]


def test_detect_all_rust(tmp_path):
    _make_rust_repo(tmp_path)
    out = detect_all(tmp_path)
    assert out["language"] == "rust"
    assert out["test_command"] == "cargo test"
    assert out["read_paths"] == ["src/**/*.rs", "tests/**/*", "Cargo.toml", "README.md"]


def test_detect_all_go(tmp_path):
    _make_go_repo(tmp_path)
    out = detect_all(tmp_path)
    assert out["language"] == "go"
    assert out["test_command"] == "go test ./..."
    assert out["read_paths"] == ["**/*.go", "go.mod", "README.md"]


def test_detect_all_unknown(tmp_path):
    out = detect_all(tmp_path)
    assert out["language"] == "unknown"
    assert out["test_command"] is None
    assert out["read_paths"] == []
    assert out["expert_roster"] == list(STANDING_ROSTER)
