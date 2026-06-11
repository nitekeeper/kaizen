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

    def test_commit_cycle_excludes_ai_dir(self, tmp_path, bare_remote, source_repo):
        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest, "main")
        create_branch(dest, "test-exclusion")
        (dest / "CHANGES.txt").write_text("a real change")
        (dest / ".ai").mkdir(exist_ok=True)
        (dest / ".ai" / "session_debug.log").write_text("debug noise")
        commit_cycle(
            clone_dir=dest,
            cycle_n=1,
            decisions=["real change"],
            participants=["Dr. Test"],
            n_tests=1,
            subject="test exclusion",
            minutes_rel_path="docs/kaizen/minutes.md",
        )
        result = subprocess.run(
            ["git", "show", "--name-only", "--pretty=format:"],
            cwd=dest,
            capture_output=True,
            text=True,
        )
        staged_files = result.stdout.strip().splitlines()
        assert "CHANGES.txt" in staged_files
        assert not any(f.startswith(".ai/") for f in staged_files), (
            f".ai/ files must not be staged: {staged_files}"
        )

    def test_commit_cycle_preserves_tracked_ai_files(self, tmp_path, bare_remote, source_repo):
        """Iron-Law (pre-fix failure): when the TARGET repo tracks files under
        .ai/, commit_cycle must not delete them — the old unconditional rmtree
        + `git add -A` committed destructive deletions of target-owned files."""
        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest, "main")
        create_branch(dest, "tracked-ai")
        # Simulate a target repo that legitimately tracks a file under .ai/.
        (dest / ".ai").mkdir()
        (dest / ".ai" / "config.json").write_text('{"target": "owned"}\n')
        subprocess.run(["git", "add", ".ai/config.json"], cwd=dest, check=True)
        subprocess.run(
            ["git", "commit", "-m", "target repo tracks .ai/config.json"],
            cwd=dest,
            check=True,
            capture_output=True,
        )
        (dest / "CHANGES.txt").write_text("a real change")
        commit_cycle(
            clone_dir=dest,
            cycle_n=1,
            decisions=["real change"],
            participants=["Dr. Test"],
            n_tests=1,
            subject="tracked ai preservation",
            minutes_rel_path="docs/kaizen/minutes.md",
        )
        # The tracked file survives on disk…
        assert (dest / ".ai" / "config.json").exists(), (
            "tracked .ai/config.json must not be deleted by commit_cycle"
        )
        # …and the cycle commit contains NO deletions.
        result = subprocess.run(
            ["git", "show", "--name-status", "--pretty=format:"],
            cwd=dest,
            capture_output=True,
            text=True,
        )
        entries = [line for line in result.stdout.strip().splitlines() if line]
        assert not any(line.startswith("D") for line in entries), (
            f"cycle commit must not contain deletions: {entries}"
        )
        assert any("CHANGES.txt" in line for line in entries)

    def test_commit_cycle_strips_nested_pycache(self, tmp_path, bare_remote, source_repo):
        """Iron-Law (pre-fix failure): __pycache__ nested below the clone root
        must be stripped before staging — the old code only removed the
        TOP-LEVEL __pycache__, letting nested .pyc files reach the PR diff."""
        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest, "main")
        create_branch(dest, "nested-pycache")
        pkg = dest / "pkg"
        (pkg / "__pycache__").mkdir(parents=True)
        (pkg / "__pycache__" / "x.pyc").write_bytes(b"\x00fake-bytecode")
        (pkg / "mod.py").write_text("x = 1\n")
        commit_cycle(
            clone_dir=dest,
            cycle_n=1,
            decisions=["add pkg"],
            participants=["Dr. Test"],
            n_tests=1,
            subject="nested pycache",
            minutes_rel_path="docs/kaizen/minutes.md",
        )
        result = subprocess.run(
            ["git", "show", "--name-only", "--pretty=format:"],
            cwd=dest,
            capture_output=True,
            text=True,
        )
        committed = result.stdout.strip().splitlines()
        assert "pkg/mod.py" in committed
        assert not any("__pycache__" in f for f in committed), (
            f"nested __pycache__ contents must not be committed: {committed}"
        )


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
