"""Tests for scripts/cycle_git.py — branch naming, commit, push."""

import re
import subprocess

from scripts.clone import clone_repo
from scripts.cycle_git import _slugify, commit_cycle, create_branch, push_branch

# ── Slugification ──────────────────────────────────────────────────────────


class TestSlugify:
    def test_simple_phrase(self):
        assert _slugify("Improve Error Handling") == "improve-error-handling"

    def test_none_returns_pm_directed(self):
        assert _slugify(None) == "pm-directed"

    def test_empty_string_returns_pm_directed(self):
        assert _slugify("") == "pm-directed"

    def test_whitespace_only_returns_pm_directed(self):
        assert _slugify("   ") == "pm-directed"

    def test_strips_non_alphanumeric(self):
        # "fix: API bug!" → "fix-api-bug"
        assert _slugify("fix: API bug!") == "fix-api-bug"

    def test_collapses_multiple_hyphens(self):
        assert _slugify("a  ---  b") == "a-b"

    def test_truncates_to_40_chars(self):
        long = "a" * 100
        slug = _slugify(long)
        assert len(slug) <= 40
        assert slug == "a" * 40

    def test_truncation_strips_trailing_hyphen(self):
        # Pick a string whose 40th char is a hyphen; ensure no dangling -
        subject = "ab-" * 20  # 'ab-ab-ab-...' length 60
        slug = _slugify(subject)
        assert not slug.endswith("-")
        assert len(slug) <= 40


# ── create_branch ──────────────────────────────────────────────────────────

_BRANCH_RE = re.compile(r"^kaizen/[a-z0-9-]+-\d{4}-\d{2}-\d{2}-\d{4}$")


class TestCreateBranch:
    def test_branch_name_format(self, tmp_path, bare_remote, source_repo):
        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest, "main")
        branch = create_branch(dest, "improve error handling")
        assert _BRANCH_RE.match(branch), f"Branch name {branch!r} does not match expected format"

    def test_branch_uses_slug(self, tmp_path, bare_remote, source_repo):
        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest, "main")
        branch = create_branch(dest, "Improve Error Handling")
        assert branch.startswith("kaizen/improve-error-handling-")

    def test_none_subject_uses_pm_directed(self, tmp_path, bare_remote, source_repo):
        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest, "main")
        branch = create_branch(dest, None)
        assert branch.startswith("kaizen/pm-directed-")
        assert _BRANCH_RE.match(branch)

    def test_branch_is_checked_out(self, tmp_path, bare_remote, source_repo):
        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest, "main")
        branch = create_branch(dest, "test subject")
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=dest,
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == branch


# ── commit_cycle ───────────────────────────────────────────────────────────


class TestCommitCycle:
    def test_commit_message_format(self, tmp_path, bare_remote, source_repo):
        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest, "main")
        create_branch(dest, "improve error handling")
        (dest / "CHANGES.txt").write_text("a change")
        commit_cycle(
            clone_dir=dest,
            cycle_n=2,
            decisions=["Improve error handling in workflow.py", "Add retry logic"],
            participants=["Dr. Priya Nair", "Dr. Nadia Petrov"],
            n_tests=7,
            subject="improve error handling",
            minutes_rel_path="docs/kaizen/2026-05-16-cycle-2-minutes.md",
        )
        result = subprocess.run(
            ["git", "log", "-1", "--pretty=%B"],
            cwd=dest,
            capture_output=True,
            text=True,
        )
        msg = result.stdout
        assert "kaizen(cycle-2):" in msg
        assert "Decisions:" in msg
        assert "1. Improve error handling in workflow.py" in msg
        assert "Tests: 7 passed" in msg
        assert "Subject: improve error handling" in msg
        assert "Dr. Priya Nair" in msg

    def test_commit_stages_all_changes(self, tmp_path, bare_remote, source_repo):
        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest, "main")
        create_branch(dest, "test")
        (dest / "new_file.txt").write_text("new")
        commit_cycle(
            clone_dir=dest,
            cycle_n=1,
            decisions=["Add file"],
            participants=["Dr. Test"],
            n_tests=1,
            subject="test",
            minutes_rel_path="docs/kaizen/minutes.md",
        )
        result = subprocess.run(
            ["git", "show", "--name-only", "--pretty=format:"],
            cwd=dest,
            capture_output=True,
            text=True,
        )
        assert "new_file.txt" in result.stdout


# ── push_branch ────────────────────────────────────────────────────────────


class TestPushBranch:
    def test_branch_appears_on_remote_after_push(self, tmp_path, bare_remote, source_repo):
        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest, "main")
        branch = create_branch(dest, "push test")
        # Need at least one commit on the branch before push has anything to send,
        # but git push of an empty branch (same as main) is fine — it sets the ref.
        (dest / "x.txt").write_text("x")
        commit_cycle(
            clone_dir=dest,
            cycle_n=1,
            decisions=["x"],
            participants=["Dr. Test"],
            n_tests=1,
            subject="push test",
            minutes_rel_path="docs/kaizen/minutes.md",
        )
        push_branch(dest, branch)
        result = subprocess.run(
            ["git", "ls-remote", "--heads", str(bare_remote)],
            capture_output=True,
            text=True,
        )
        assert branch in result.stdout
