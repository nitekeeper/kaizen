"""Git worktree merge-back and cleanup for session close."""

from __future__ import annotations

import sys
from pathlib import Path

from scripts.git_utils import git as _git


def detect_worktree(cwd: Path) -> tuple[bool, str]:
    """Return (is_linked_worktree, git_dir_path)."""
    result = _git(["rev-parse", "--git-dir"], cwd)
    git_dir = result.stdout.strip()
    is_worktree = "worktrees" in git_dir.replace("\\", "/")
    return is_worktree, git_dir


def get_current_branch(cwd: Path) -> str:
    result = _git(["branch", "--show-current"], cwd)
    return result.stdout.strip()


def classify_status(porcelain_output: str) -> tuple[list[str], list[str], list[str]]:
    """Split `git status --porcelain` lines into (dirty_tracked, untracked_claude, untracked_other).

    `.claude/` is Claude Code's worktree-local harness storage — not project files,
    so it is broken out from the other-untracked bucket and ignored by sync flows.
    """
    lines = porcelain_output.splitlines()
    dirty = [ln for ln in lines if not ln.startswith("??")]
    untracked_claude = [
        ln for ln in lines if ln.startswith("?? ") and ln[3:].startswith(".claude/")
    ]
    untracked_other = [
        ln for ln in lines if ln.startswith("?? ") and not ln[3:].startswith(".claude/")
    ]
    return dirty, untracked_claude, untracked_other


def parse_main_worktree(cwd: Path) -> tuple[str, str]:
    """Return (main_worktree_path, base_branch) from the first entry in worktree list."""
    result = _git(["worktree", "list", "--porcelain"], cwd)
    normalised = result.stdout.replace("\r\n", "\n").replace("\r", "\n")
    blocks = normalised.strip().split("\n\n")
    main_block = blocks[0]
    worktree_path = ""
    branch = ""
    for line in main_block.splitlines():
        if line.startswith("worktree "):
            worktree_path = line.split(" ", 1)[1]
        elif line.startswith("branch "):
            branch = line.split(" ", 1)[1].replace("refs/heads/", "")
    if not worktree_path:
        raise RuntimeError("Could not determine main worktree path from `git worktree list`")
    if not branch:
        branch = "main"
    return worktree_path, branch


def merge_back(worktree_dir: Path) -> None:
    """
    From inside a linked worktree: commit any pending changes, then merge
    the worktree branch into the base branch in the main workspace, and
    delete the worktree and its branch.

    Raises RuntimeError with clear instructions on merge conflict, dirty main
    workspace, or detached HEAD. Unlike auto_merge_to_main, this does NOT
    stash — the developer is present and should handle their own workspace state.
    """
    # ── Detect ───────────────────────────────────────────────────────────────
    is_worktree, _ = detect_worktree(worktree_dir)
    if not is_worktree:
        print("Not in a linked worktree. Nothing to merge back.")
        return

    wt_branch = get_current_branch(worktree_dir)
    if not wt_branch:
        raise RuntimeError(
            "ERROR: Worktree is in detached HEAD state. "
            "Re-attach to a branch before saving:\n"
            "  git checkout -b <branch-name>"
        )

    main_path_str, base_branch = parse_main_worktree(worktree_dir)
    main_path = Path(main_path_str)

    # ── Commit pending changes in the worktree ───────────────────────────────
    status = _git(["status", "--porcelain"], worktree_dir)
    if status.stdout.strip():
        _git(["add", "-A"], worktree_dir)
        _git(["commit", "-m", f"chore: save session state [{wt_branch}]"], worktree_dir)
        print(f"Committed pending changes on {wt_branch}.")
    else:
        print("No uncommitted changes in worktree.")

    # ── Pre-flight: main workspace must be clean and on the base branch ──────
    main_current = get_current_branch(main_path)
    if main_current != base_branch:
        raise RuntimeError(
            f"ERROR: Main workspace is on '{main_current}', not '{base_branch}'.\n"
            f"Check out '{base_branch}' in the main workspace before running save:\n"
            f"  cd {main_path}\n"
            f"  git checkout {base_branch}"
        )

    main_status = _git(["status", "--porcelain"], main_path)
    dirty_lines, untracked_claude, untracked_other = classify_status(main_status.stdout)

    if dirty_lines or untracked_other:
        raise RuntimeError(
            f"ERROR: Main workspace has uncommitted changes.\n"
            f"Commit or stash them first:\n"
            f"  cd {main_path}\n"
            f"  git stash"
        )

    if untracked_claude:
        print(
            f"Note: Untracked .claude/ files detected and skipped — these are Claude Code's worktree storage, not project files.\n"
            f"To silence this warning permanently, add '.claude/' to {main_path}/.git/info/exclude"
        )

    # ── Merge ────────────────────────────────────────────────────────────────
    merge_result = _git(
        ["merge", "--no-ff", wt_branch, "-m", f"Merge {wt_branch} into {base_branch}"],
        main_path,
        check=False,
    )
    if merge_result.returncode != 0:
        _git(["merge", "--abort"], main_path, check=False)
        raise RuntimeError(
            f"CONFLICT: Merge of '{wt_branch}' into '{base_branch}' produced conflicts.\n"
            f"The merge has been aborted. Your worktree and branch are intact.\n\n"
            f"To resolve manually:\n"
            f"  cd {main_path}\n"
            f"  git merge --no-ff {wt_branch}\n"
            f"  # resolve conflicts, then:\n"
            f"  git merge --continue\n"
            f"  git worktree remove {worktree_dir}\n"
            f"  git branch -d {wt_branch}"
        )

    print(f"Merged '{wt_branch}' into '{base_branch}'.")

    # ── Remove worktree ───────────────────────────────────────────────────────
    worktree_path_str = str(worktree_dir.resolve())
    rm_result = _git(
        ["worktree", "remove", worktree_path_str, "--force"],
        main_path,
        check=False,
    )
    if rm_result.returncode != 0:
        # Path may already be gone; prune stale entries
        _git(["worktree", "prune"], main_path, check=False)
        print("Worktree path already removed or inaccessible; ran `git worktree prune`.")
    else:
        print(f"Worktree '{worktree_path_str}' removed.")

    # ── Delete branch (safe: -d refuses unmerged) ────────────────────────────
    branch_del = _git(["branch", "-d", wt_branch], main_path, check=False)
    if branch_del.returncode != 0:
        # Branch may already not exist
        ref_check = _git(
            ["show-ref", "--verify", "--quiet", f"refs/heads/{wt_branch}"],
            main_path,
            check=False,
        )
        if ref_check.returncode != 0:
            print(f"Branch '{wt_branch}' already deleted.")
        else:
            print(
                f"WARNING: Could not delete branch '{wt_branch}' with -d.\n"
                f"If you are sure it was merged, run:\n"
                f"  git branch -D {wt_branch}"
            )
    else:
        print(f"Branch '{wt_branch}' deleted.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "merge-back":
        try:
            merge_back(Path.cwd())
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)
    else:
        print("Commands: merge-back", file=sys.stderr)
        sys.exit(1)
