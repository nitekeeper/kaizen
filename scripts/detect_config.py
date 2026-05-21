"""Pure detection module: infer project language, test command, read paths,
and a default expert roster from an on-disk repo. No I/O prompts — call sites
handle confirmation."""

from __future__ import annotations

import json
from pathlib import Path

# Standing 6 — always present regardless of language.
STANDING_ROSTER = [
    "agent-systems-architect-1",
    "ai-safety-researcher-1",
    "prompt-engineer-1",
    "ai-ethicist-1",
    "ai-research-scientist-1",
    "cognitive-scientist-1",
]

LANGUAGE_EXTRA_ROSTER = {
    "python": ["backend-engineer-1", "data-engineer-1"],
    "javascript": ["frontend-engineer-1", "fullstack-engineer-1"],
    "rust": ["systems-architect-1", "backend-engineer-1"],
    "go": ["backend-engineer-1", "software-architect-1"],
}

READ_PATHS = {
    "python": ["scripts/*.py", "tests/*.py", "skills/*/SKILL.md", "CLAUDE.md", "README.md"],
    "javascript": ["src/**/*.{js,ts}", "test/**/*", "README.md"],
    "rust": ["src/**/*.rs", "tests/**/*", "Cargo.toml", "README.md"],
    "go": ["**/*.go", "go.mod", "README.md"],
    "unknown": [],
}


def detect_language(repo_root: Path) -> str:
    """Return one of: python, javascript, rust, go, unknown. First match wins."""
    repo_root = Path(repo_root)
    if (
        (repo_root / "pyproject.toml").exists()
        or (repo_root / "pytest.ini").exists()
        or (repo_root / "setup.py").exists()
    ):
        return "python"
    package_json = repo_root / "package.json"
    if package_json.exists():
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
            if isinstance(data.get("scripts"), dict) and "test" in data["scripts"]:
                return "javascript"
        except (json.JSONDecodeError, OSError):
            pass
    if (repo_root / "Cargo.toml").exists():
        return "rust"
    if (repo_root / "go.mod").exists():
        return "go"
    return "unknown"


def detect_test_command(repo_root: Path, language: str) -> str | None:
    """Return the test command for the language, or None for unknown."""
    repo_root = Path(repo_root)
    if language == "python":
        return "pytest -v --tb=short"
    if language == "javascript":
        package_json = repo_root / "package.json"
        if package_json.exists():
            try:
                data = json.loads(package_json.read_text(encoding="utf-8"))
                scripts = data.get("scripts") or {}
                if isinstance(scripts, dict) and scripts.get("test"):
                    return scripts["test"]
            except (json.JSONDecodeError, OSError):
                pass
        return "npm test"
    if language == "rust":
        return "cargo test"
    if language == "go":
        return "go test ./..."
    return None


def detect_read_paths(repo_root: Path, language: str) -> list[str]:
    """Return the default read_paths glob list for the language."""
    return list(READ_PATHS.get(language, []))


def default_expert_roster(language: str) -> list[str]:
    """Standing 6 + language-specific specialists."""
    return list(STANDING_ROSTER) + list(LANGUAGE_EXTRA_ROSTER.get(language, []))


def detect_all(repo_root: Path) -> dict:
    """Convenience aggregator."""
    repo_root = Path(repo_root)
    language = detect_language(repo_root)
    return {
        "language": language,
        "test_command": detect_test_command(repo_root, language),
        "read_paths": detect_read_paths(repo_root, language),
        "expert_roster": default_expert_roster(language),
    }
