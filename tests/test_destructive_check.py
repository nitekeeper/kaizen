"""Tests for scripts/destructive_check.py (vendored verbatim from atelier)."""

import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.destructive_check import (
    _check_removed_public_functions,
    _deleted_file_paths,
    _is_imported_by_any_file,
    detect_destructive,
    get_diff,
)


def _delete_diff(filepath: str, body: str = "pass") -> str:
    """Build a minimal unified diff that deletes filepath."""
    lines = body.splitlines()
    removed = "\n".join(f"-{line}" for line in lines)
    return textwrap.dedent(f"""\
        diff --git a/{filepath} b/{filepath}
        deleted file mode 100644
        index abc123..0000000
        --- a/{filepath}
        +++ /dev/null
        @@ -1,{len(lines)} +0,0 @@
        {removed}
    """)


# ── _deleted_file_paths ────────────────────────────────────────────────────


class TestDeletedFilePaths:
    def test_single_deleted_file(self):
        diff = _delete_diff("scripts/db.py", "def get_connection(): pass")
        assert _deleted_file_paths(diff) == ["scripts/db.py"]

    def test_no_deleted_files(self):
        diff = "+new line\n-old line\n"
        assert _deleted_file_paths(diff) == []

    def test_multiple_deleted_files(self):
        diff = _delete_diff("scripts/db.py") + "\n" + _delete_diff("scripts/old.py")
        paths = _deleted_file_paths(diff)
        assert "scripts/db.py" in paths
        assert "scripts/old.py" in paths


# ── _is_imported_by_any_file ───────────────────────────────────────────────


class TestIsImportedByAnyFile:
    def test_imported_via_from_import(self, tmp_path):
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "workflow.py").write_text("from scripts.db import get_connection\n")
        assert _is_imported_by_any_file("scripts/db.py", tmp_path) is True

    def test_not_imported(self, tmp_path):
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "workflow.py").write_text("x = 1\n")
        assert _is_imported_by_any_file("scripts/db.py", tmp_path) is False

    def test_markdown_file_never_imported(self, tmp_path):
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "workflow.py").write_text("from scripts.db import get_connection\n")
        assert _is_imported_by_any_file("docs/readme.md", tmp_path) is False

    def test_git_dir_not_scanned(self, tmp_path):
        git_hooks = tmp_path / ".git" / "hooks"
        git_hooks.mkdir(parents=True)
        (git_hooks / "pre-commit").write_text("from scripts.db import get_connection\n")
        diff = _delete_diff("scripts/db.py", "def get_connection(): pass")
        issues = detect_destructive(diff, tmp_path)
        assert not any(i["type"] == "deleted_imported_file" for i in issues)


# ── detect_destructive ─────────────────────────────────────────────────────


class TestDetectDestructive:
    def test_deleted_imported_file_flagged(self, tmp_path):
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "workflow.py").write_text("from scripts.db import get_connection\n")
        diff = _delete_diff("scripts/db.py", "def get_connection(): pass")
        issues = detect_destructive(diff, tmp_path)
        assert any(i["type"] == "deleted_imported_file" for i in issues)

    def test_deleted_non_imported_file_not_flagged(self, tmp_path):
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "workflow.py").write_text("x = 1\n")
        diff = _delete_diff("docs/old_readme.md", "# old")
        issues = detect_destructive(diff, tmp_path)
        assert not any(i["type"] == "deleted_imported_file" for i in issues)

    def test_removed_public_function_flagged(self, tmp_path):
        diff = _make_modify_diff("scripts/workflow.py", ["def get_phase(project_id):", "    pass"])
        issues = detect_destructive(diff, tmp_path)
        assert any(i["type"] == "removed_public_function" for i in issues)

    def test_removed_private_function_not_flagged(self, tmp_path):
        diff = "-def _internal_helper():\n-    pass\n"
        issues = detect_destructive(diff, tmp_path)
        assert not any(i["type"] == "removed_public_function" for i in issues)

    def test_removed_class_method_not_flagged(self, tmp_path):
        diff = "-    def get_phase(self):\n-        pass\n"
        issues = detect_destructive(diff, tmp_path)
        assert not any(i["type"] == "removed_public_function" for i in issues)

    def test_db_drop_table_flagged(self, tmp_path):
        diff = "+DROP TABLE sessions;\n"
        issues = detect_destructive(diff, tmp_path)
        assert any(i["type"] == "destructive_db_migration" for i in issues)

    def test_db_drop_column_flagged(self, tmp_path):
        diff = "+ALTER TABLE projects DROP COLUMN description;\n"
        issues = detect_destructive(diff, tmp_path)
        assert any(i["type"] == "destructive_db_migration" for i in issues)

    def test_db_drop_in_sql_comment_not_flagged(self, tmp_path):
        diff = "+-- DROP TABLE old_table;\n"
        issues = detect_destructive(diff, tmp_path)
        assert not any(i["type"] == "destructive_db_migration" for i in issues)

    def test_removed_skill_dir_flagged(self, tmp_path):
        diff = _delete_diff("skills/dev-qa/SKILL.md", "# dev:qa")
        issues = detect_destructive(diff, tmp_path)
        assert any(i["type"] == "removed_skill_directory" for i in issues)

    def test_removed_test_file_flagged(self, tmp_path):
        diff = _delete_diff("tests/test_workflow.py", "def test_x(): pass")
        issues = detect_destructive(diff, tmp_path)
        assert any(i["type"] == "removed_test_file" for i in issues)

    def test_no_destructive_changes_returns_empty(self, tmp_path):
        diff = "+new line added\n+another addition\n"
        issues = detect_destructive(diff, tmp_path)
        assert issues == []

    def test_returns_list_of_dicts_with_required_keys(self, tmp_path):
        diff = "+DROP TABLE sessions;\n"
        issues = detect_destructive(diff, tmp_path)
        for issue in issues:
            assert "type" in issue
            assert "description" in issue
            assert "file" in issue


def _make_modify_diff(
    filepath: str, removed_lines: list[str], added_lines: list[str] | None = None
) -> str:
    """Build a minimal unified diff that modifies filepath (not a file deletion)."""
    added_lines = added_lines or []
    removed = "\n".join(f"-{line}" for line in removed_lines)
    added = "\n".join(f"+{line}" for line in added_lines)
    hunk_body = "\n".join(filter(None, [removed, added]))
    total_old = len(removed_lines)
    total_new = len(added_lines)
    return (
        f"diff --git a/{filepath} b/{filepath}\n"
        f"index abc123..def456 100644\n"
        f"--- a/{filepath}\n"
        f"+++ b/{filepath}\n"
        f"@@ -{total_old},0 +{total_new},0 @@\n"
        f"{hunk_body}\n"
    )


# ── _check_removed_public_functions: .py extension gate (M4, task 16) ─────


class TestCheckRemovedPublicFunctionsPyExtensionGate:
    """Gate tests: def removal check must only fire for .py files."""

    def test_py_file_def_removal_produces_issue(self):
        """Happy path: a .py file with a removed public def produces an issue."""
        diff = _make_modify_diff(
            "scripts/workflow.py",
            ["def get_phase(project_id):"],
        )
        issues = _check_removed_public_functions(diff)
        assert any(i["type"] == "removed_public_function" for i in issues)
        assert issues[0]["file"] == "scripts/workflow.py"

    def test_markdown_file_def_removal_no_issue(self):
        """Markdown file containing '-def fake(' must NOT produce an issue."""
        diff = _make_modify_diff(
            "README.md",
            ["def fake(x):"],
        )
        issues = _check_removed_public_functions(diff)
        assert issues == [], f"Expected no issues for .md file, got: {issues}"

    def test_json_file_def_removal_no_issue(self):
        """JSON file containing '-def something(' must NOT produce an issue."""
        diff = _make_modify_diff(
            "fixtures/data.json",
            ["def something(arg):"],
        )
        issues = _check_removed_public_functions(diff)
        assert issues == [], f"Expected no issues for .json file, got: {issues}"

    def test_mixed_diff_only_py_file_produces_issue(self):
        """A diff with both a .py def removal and a .md def removal yields exactly one issue."""
        diff = _make_modify_diff("scripts/workflow.py", ["def real_func(x):"]) + _make_modify_diff(
            "docs/guide.md", ["def fake_func(y):"]
        )
        issues = _check_removed_public_functions(diff)
        assert len(issues) == 1, f"Expected exactly 1 issue, got: {issues}"
        assert issues[0]["file"] == "scripts/workflow.py"
        assert issues[0]["type"] == "removed_public_function"

    def test_uppercase_py_extension_produces_issue(self):
        """A file with .PY extension (case-insensitive match) produces an issue."""
        diff = _make_modify_diff(
            "legacy/FOO.PY",
            ["def bar(x):"],
        )
        issues = _check_removed_public_functions(diff)
        assert any(i["type"] == "removed_public_function" for i in issues), (
            "Expected .PY (uppercase) to be treated as Python file"
        )

    def test_unknown_file_no_issue(self):
        """Lines before any diff header (current_file == 'unknown') are skipped."""
        # No 'diff --git' header — current_file stays 'unknown', suffix != '.py'
        raw_diff = "-def orphan(x):\n-    pass\n"
        issues = _check_removed_public_functions(raw_diff)
        assert issues == [], f"Expected no issues for unknown file context, got: {issues}"


# ── get_diff library error path ────────────────────────────────────────────


class TestGetDiff:
    def test_raises_runtime_error_when_git_diff_fails(self, tmp_path):
        """get_diff() must raise RuntimeError (not sys.exit) when git diff fails."""
        failed = subprocess.CompletedProcess(
            args=["git", "diff", "HEAD"],
            returncode=1,
            stdout="",
            stderr="fatal: not a git repository",
        )
        with (
            patch("scripts.destructive_check.subprocess.run", return_value=failed),
            pytest.raises(RuntimeError, match="git diff failed"),
        ):
            get_diff(tmp_path)

    def test_runtime_error_includes_stderr(self, tmp_path):
        """The RuntimeError message must embed the captured stderr text."""
        failed = subprocess.CompletedProcess(
            args=["git", "diff", "HEAD"],
            returncode=1,
            stdout="",
            stderr="fatal: not a git repository",
        )
        with (
            patch("scripts.destructive_check.subprocess.run", return_value=failed),
            pytest.raises(RuntimeError, match="fatal: not a git repository"),
        ):
            get_diff(tmp_path)


# ── CLI translation for destructive_check ─────────────────────────────────


class TestDestructiveCheckCLI:
    def test_cli_exits_1_and_writes_stderr_on_bad_path(self, tmp_path):
        """CLI guard must catch RuntimeError and exit 1 with a stderr message."""
        # Pass a path that is not a git repo so git diff fails
        non_git = tmp_path / "not_a_repo"
        non_git.mkdir()
        result = subprocess.run(
            [sys.executable, "scripts/destructive_check.py", str(non_git)],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        assert result.returncode == 1
        assert result.stderr.strip() != ""
