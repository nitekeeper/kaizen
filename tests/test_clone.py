"""Tests for scripts/clone.py — clone + cleanup."""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

# ── clone_repo ─────────────────────────────────────────────────────────────


class TestCloneRepo:
    def test_clone_creates_directory_with_contents(self, tmp_path, bare_remote, source_repo):
        from scripts.clone import clone_repo

        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest, "main")
        assert dest.exists()
        assert (dest / "README.md").exists()

    def test_clone_sets_git_identity(self, tmp_path, bare_remote, source_repo):
        from scripts.clone import clone_repo

        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest, "main")
        result = subprocess.run(
            ["git", "config", "user.email"],
            cwd=dest,
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "kaizen@kaizen.local"

    def test_clone_accepts_url_directly(self, tmp_path, bare_remote, source_repo):
        """clone_repo must take the URL as argument, not look up origin."""
        from scripts.clone import clone_repo

        dest = tmp_path / "clone"
        # Pass the bare_remote path as the URL directly — no origin lookup.
        clone_repo(str(bare_remote), dest, "main")
        assert (dest / "README.md").exists()

    def test_clone_honors_non_main_branch(self, tmp_path, bare_remote_trunk):
        """Branch argument must be honored end-to-end (not silently ignored)."""
        from scripts.clone import clone_repo

        dest = tmp_path / "clone_trunk"
        clone_repo(str(bare_remote_trunk), dest, "trunk")

        assert dest.exists()
        assert (dest / "README.md").exists()

        head = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=dest,
            capture_output=True,
            text=True,
            check=True,
        )
        assert head.stdout.strip() == "trunk"

    def test_clone_failure_raises_enriched_error(self, tmp_path):
        """Clone failures (auth/bad URL/network) must surface stderr in str(exc):
        run.py persists str(exc) on the critical path."""
        from scripts.clone import clone_repo

        missing = tmp_path / "no-such-remote.git"
        with pytest.raises(subprocess.CalledProcessError) as excinfo:
            clone_repo(str(missing), tmp_path / "dest", "main")
        msg = str(excinfo.value)
        assert str(missing) in msg
        # Pre-fix str(CalledProcessError) carries no stderr at all.
        assert "fatal" in msg.lower() or "does not exist" in msg.lower()

    def test_clone_rejects_empty_branch(self, tmp_path):
        """Empty branch string must surface a clear ValueError, not a vague git error."""
        from scripts.clone import clone_repo

        with pytest.raises(ValueError, match="branch must be a non-empty string"):
            clone_repo("https://example.com/x.git", tmp_path / "clone", "")

    def test_clone_yields_deterministic_lf_working_copy_under_autocrlf(self, tmp_path):
        """M8b finding: clone must produce LF working copies even when the host's
        GLOBAL git config sets core.autocrlf=true.

        Root cause: on a WSL/Windows host with global core.autocrlf=true a full
        `git clone` checks out CRLF working copies against LF-committed blobs.
        A later git op running under a DIFFERENT autocrlf (e.g.
        GIT_CONFIG_GLOBAL=/dev/null, or a target .gitattributes) then reads the
        base tree as DIRTY — which makes atelier's engine `git merge --no-ff` of a
        worktree that MODIFIES an existing file refuse and crash the host cycle.

        The fix clones with --no-checkout, sets local core.autocrlf=false, then
        checks out — yielding deterministic LF working copies that match the LF
        blobs and read CLEAN under any later autocrlf.

        Host-INDEPENDENT: we force autocrlf=true via a temp GLOBAL gitconfig
        (GIT_CONFIG_GLOBAL) for the clone, so the bug reproduces on any host
        regardless of the developer's own global config. The PRIMARY signal is the
        actual working-copy BYTES (LF vs CRLF) — `git status` alone can be masked
        by the racy-git stat cache — backed by a /dev/null-global status check.
        """
        from scripts.clone import clone_repo

        # ── Build a SOURCE repo whose committed blob is LF (committed with
        #    autocrlf OFF so the blob bytes are exactly LF). ──────────────────
        source = tmp_path / "source"
        source.mkdir()
        src_env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,  # neutral global for the seed commit
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }

        def _src_git(*args):
            subprocess.run(["git", *args], cwd=source, env=src_env, check=True, capture_output=True)

        _src_git("init", "-q", "-b", "main")
        _src_git("config", "core.autocrlf", "false")
        # Multi-line LF content — written with explicit LF bytes (newline="") so
        # the committed blob is unambiguously LF on every platform.
        lf_text = "line one\nline two\nline three\n"
        (source / "existing.txt").write_text(lf_text, newline="")
        _src_git("add", "-A")
        _src_git("commit", "-qm", "seed LF file")

        # ── Clone via clone_repo with a GLOBAL autocrlf=true (the host bug
        #    trigger). The fix must neutralize it locally. ────────────────────
        autocrlf_global = tmp_path / "autocrlf_true.gitconfig"
        autocrlf_global.write_text("[core]\n\tautocrlf = true\n")

        dest = tmp_path / "clone"
        prev_global = os.environ.get("GIT_CONFIG_GLOBAL")
        os.environ["GIT_CONFIG_GLOBAL"] = str(autocrlf_global)
        try:
            clone_repo(str(source), dest, "main")
        finally:
            if prev_global is None:
                os.environ.pop("GIT_CONFIG_GLOBAL", None)
            else:
                os.environ["GIT_CONFIG_GLOBAL"] = prev_global

        # ── (a) PRIMARY signal: the working-copy bytes are LF, not CRLF. ─────
        raw = (dest / "existing.txt").read_bytes()
        assert b"\r\n" not in raw, (
            "cloned working copy has CRLF line endings — a full clone under "
            "global autocrlf=true re-encoded the LF blob to CRLF on checkout"
        )
        assert raw == lf_text.encode("utf-8"), (
            f"cloned working copy bytes != committed LF bytes: {raw!r}"
        )

        # ── (b) status is CLEAN even under GIT_CONFIG_GLOBAL=/dev/null (the
        #    autocrlf a later git op might run under). A CRLF working copy
        #    against an LF blob reads DIRTY here. ──────────────────────────────
        status = subprocess.run(
            ["git", "-C", str(dest), "status", "--porcelain"],
            env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull},
            capture_output=True,
            text=True,
            check=True,
        )
        assert status.stdout.strip() == "", (
            "cloned base tree reads DIRTY under GIT_CONFIG_GLOBAL=/dev/null: "
            f"{status.stdout!r} — engine `git merge --no-ff` would refuse on a "
            "file-modifying worktree"
        )


# ── get_remote_url ─────────────────────────────────────────────────────────


class TestGetRemoteUrl:
    def test_returns_origin_url(self, tmp_path, bare_remote, source_repo):
        from scripts.clone import clone_repo, get_remote_url

        dest = tmp_path / "clone"
        clone_repo(str(bare_remote), dest, "main")
        url = get_remote_url(dest)
        assert str(bare_remote) in url


# ── cleanup_experiment ─────────────────────────────────────────────────────


class TestCleanupExperiment:
    def test_removes_directory_recursively(self, tmp_path):
        from scripts.clone import cleanup_experiment

        exp = tmp_path / "experiment"
        (exp / "target").mkdir(parents=True)
        (exp / "target" / "file.txt").write_text("x")
        cleanup_experiment(exp)
        assert not exp.exists()

    def test_no_error_if_already_absent(self, tmp_path):
        from scripts.clone import cleanup_experiment

        cleanup_experiment(tmp_path / "nonexistent")  # must not raise

    def test_accepts_str_path(self, tmp_path):
        from scripts.clone import cleanup_experiment

        exp = tmp_path / "experiment"
        (exp / "target").mkdir(parents=True)
        (exp / "target" / "file.txt").write_text("x")
        cleanup_experiment(str(exp))  # str not Path — called from skill bash glue
        assert not exp.exists()

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only readonly attribute bug")
    def test_cleanup_removes_readonly_files_on_windows(self, tmp_path):
        """Read-only files (like git pack objects) must not block cleanup on Windows."""
        from scripts.clone import cleanup_experiment

        exp = tmp_path / "experiment"
        (exp / "sub").mkdir(parents=True)
        readonly_file = exp / "sub" / "readonly.txt"
        readonly_file.write_text("locked")
        os.chmod(readonly_file, stat.S_IREAD)

        cleanup_experiment(exp)
        assert not exp.exists()


# ── CLI ────────────────────────────────────────────────────────────────────


class TestCLI:
    def test_unknown_command_exits_1(self):
        result = subprocess.run(
            [sys.executable, "scripts/clone.py", "bogus"],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": "."},
        )
        assert result.returncode == 1

    def test_clone_cli_works(self, tmp_path, bare_remote, source_repo):
        dest = tmp_path / "clone"
        result = subprocess.run(
            [
                sys.executable,
                str(Path.cwd() / "scripts" / "clone.py"),
                "clone",
                str(bare_remote),
                "main",
                str(dest),
            ],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(Path.cwd())},
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert dest.exists()

    def test_cleanup_cli_works(self, tmp_path):
        exp = tmp_path / "experiment"
        exp.mkdir()
        result = subprocess.run(
            [sys.executable, str(Path.cwd() / "scripts" / "clone.py"), "cleanup", str(exp)],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(Path.cwd())},
        )
        assert result.returncode == 0
        assert not exp.exists()
