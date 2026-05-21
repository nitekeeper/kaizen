"""Tests for scripts/destructive_check.py (vendored verbatim from atelier)."""

import textwrap

from scripts.destructive_check import (
    _deleted_file_paths,
    _is_imported_by_any_file,
    detect_destructive,
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
        diff = "-def get_phase(project_id):\n-    pass\n"
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
