"""Detect destructive changes in a git diff for Atelier self-improvement cycles."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


def get_diff(clone_dir: Path) -> str:
    """Return the full diff of all changes against HEAD in the clone."""
    result = subprocess.run(
        ["git", "diff", "HEAD"],
        cwd=clone_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Error: git diff failed in {clone_dir}: {result.stderr.strip()}")
    return result.stdout


def _deleted_file_paths(diff_text: str) -> list[str]:
    """Extract paths of deleted files from a git diff."""
    pattern = re.compile(r"^diff --git a/(.+?) b/\1\r?\ndeleted file mode", re.MULTILINE)
    return [m.group(1) for m in pattern.finditer(diff_text)]


def _is_imported_by_any_file(filepath: str, repo_dir: Path) -> bool:
    """Return True if any Python file in repo_dir imports filepath."""
    if not filepath.endswith(".py"):
        return False
    module_parts = Path(filepath).with_suffix("").parts
    import_patterns = [
        f"from {'.'.join(module_parts)} import",
        f"import {'.'.join(module_parts)}",
        f"from {module_parts[-1]} import",
        f"import {module_parts[-1]}",
    ]
    for py_file in repo_dir.rglob("*.py"):
        if ".git" in py_file.parts:
            continue
        try:
            content = py_file.read_text(encoding="utf-8")
            for line in content.splitlines():
                if line.lstrip().startswith("#"):
                    continue
                if any(p in line for p in import_patterns):
                    return True
        except OSError:
            continue
    return False


def _check_deleted_files(diff_text: str, repo_dir: Path) -> list[dict]:
    """Flag deleted Python files that are imported by other files."""
    issues = []
    for path in _deleted_file_paths(diff_text):
        if _is_imported_by_any_file(path, repo_dir):
            issues.append(
                {
                    "type": "deleted_imported_file",
                    "description": f"Deleted '{path}' is imported by other files",
                    "file": path,
                }
            )
    return issues


def _check_removed_public_functions(diff_text: str) -> list[dict]:
    """Flag removed top-level public function definitions (not starting with _).

    Two-pass per-file pairing: a ``-def name`` line is only flagged when the
    SAME file's diff does not also add a ``+def name`` — a removed/added pair
    is a signature change (or in-file move), not a removal. Renames still
    flag the old name; a cross-file move still flags the removal in the
    source file (a same-name ``+def`` in another file does not suppress it).
    """
    removed_re = re.compile(r"^-(?:async\s+)?def ([a-zA-Z][a-zA-Z0-9_]*)\(")
    added_re = re.compile(r"^\+(?:async\s+)?def ([a-zA-Z][a-zA-Z0-9_]*)\(")
    # Pass 1 — collect removed/added def names per file.
    removed_by_file: dict[str, list[str]] = {}
    added_by_file: dict[str, set[str]] = {}
    current_file = "unknown"
    for line in diff_text.splitlines():
        header = re.match(r"^diff --git a/(.+?) b/\1$", line)
        if header:
            current_file = header.group(1)
        if Path(current_file).suffix.lower() != ".py":
            continue
        m = removed_re.match(line)
        if m:
            removed_by_file.setdefault(current_file, []).append(m.group(1))
            continue
        m = added_re.match(line)
        if m:
            added_by_file.setdefault(current_file, set()).add(m.group(1))
    # Pass 2 — flag only removals with no same-name +def in the same file.
    issues = []
    for filepath, names in removed_by_file.items():
        added_names = added_by_file.get(filepath, set())
        for name in names:
            if name in added_names:
                continue
            issues.append(
                {
                    "type": "removed_public_function",
                    "description": f"Public function '{name}' was removed",
                    "file": filepath,
                }
            )
    return issues


def _check_db_migrations(diff_text: str) -> list[dict]:
    """Flag SQL that drops or renames tables/columns (skips SQL comments)."""
    issues = []
    pattern = re.compile(
        r"^\+.*(DROP\s+TABLE|DROP\s+COLUMN|RENAME\s+TABLE|RENAME\s+COLUMN"
        r"|ALTER\s+TABLE\s+\w+\s+RENAME)",
        re.IGNORECASE | re.MULTILINE,
    )
    for m in pattern.finditer(diff_text):
        line = m.group(0).lstrip("+").lstrip()
        if line.startswith("--"):
            continue
        issues.append(
            {
                "type": "destructive_db_migration",
                "description": f"Destructive SQL: {m.group(0).strip()}",
                "file": "migration",
            }
        )
    return issues


def _check_removed_skill_dirs(diff_text: str) -> list[dict]:
    """Flag deleted SKILL.md files (skill directory removed)."""
    issues = []
    for path in _deleted_file_paths(diff_text):
        if path.startswith("skills/") and path.endswith("SKILL.md"):
            issues.append(
                {
                    "type": "removed_skill_directory",
                    "description": f"Skill '{Path(path).parent.name}' was removed",
                    "file": path,
                }
            )
    return issues


def _check_removed_tests(diff_text: str) -> list[dict]:
    """Flag deleted test files."""
    issues = []
    for path in _deleted_file_paths(diff_text):
        if Path(path).name.startswith("test_") and path.endswith(".py"):
            issues.append(
                {
                    "type": "removed_test_file",
                    "description": f"Test file '{path}' was deleted",
                    "file": path,
                }
            )
    return issues


def detect_destructive(diff_text: str, repo_dir: Path) -> list[dict]:
    """
    Scan a git diff for destructive changes.

    Returns a list of dicts with keys: type, description, file.
    Empty list means no destructive changes detected.
    """
    issues: list[dict] = []
    issues.extend(_check_deleted_files(diff_text, repo_dir))
    issues.extend(_check_removed_public_functions(diff_text))
    issues.extend(_check_db_migrations(diff_text))
    issues.extend(_check_removed_skill_dirs(diff_text))
    issues.extend(_check_removed_tests(diff_text))
    return issues


if __name__ == "__main__":
    # CLI: python3 scripts/destructive_check.py <clone_dir>
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/destructive_check.py <clone_dir>")
        sys.exit(1)
    clone_dir = Path(sys.argv[1])
    try:
        diff = get_diff(clone_dir)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    issues = detect_destructive(diff, clone_dir)
    print(json.dumps(issues, indent=2))
    if issues:
        sys.exit(1)
