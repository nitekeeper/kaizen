"""Kaizen cycle git operations — branch, commit, push.

Branch naming format:
    kaizen/<subject-slug-or-pm-directed>-YYYY-MM-DD-HHMM
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from scripts.git_utils import git as _git

# ── Slug helper ────────────────────────────────────────────────────────────

_MAX_SLUG_LEN = 40


def _slugify(subject: str | None) -> str:
    """Slugify a subject string for use in a branch name.

    Lowercase, spaces → hyphens, strip non-alphanumeric except hyphens,
    collapse repeated hyphens, trim hyphens, truncate to 40 chars.
    When subject is None or empty after cleaning, returns 'pm-directed'.
    """
    if subject is None:
        return "pm-directed"
    s = subject.strip().lower()
    # Replace whitespace runs with a single hyphen
    s = re.sub(r"\s+", "-", s)
    # Drop anything that isn't alphanumeric or hyphen
    s = re.sub(r"[^a-z0-9-]", "", s)
    # Collapse repeated hyphens
    s = re.sub(r"-+", "-", s)
    # Trim leading/trailing hyphens
    s = s.strip("-")
    if not s:
        return "pm-directed"
    return s[:_MAX_SLUG_LEN].rstrip("-") or "pm-directed"


# ── Public functions ───────────────────────────────────────────────────────


def create_branch(clone_dir: Path, subject: str | None) -> str:
    """Create and checkout kaizen/<slug>-YYYY-MM-DD-HHMM. Returns branch name.

    subject is slugified; when None, uses 'pm-directed'.
    Timestamp is UTC.
    """
    now = datetime.now(UTC)
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H%M")
    slug = _slugify(subject)
    branch = f"kaizen/{slug}-{date_str}-{time_str}"
    _git(["checkout", "-b", branch], clone_dir)
    return branch


def _tracked_under(clone_dir: Path, rel: str) -> bool:
    """Return True when git tracks any file at/under ``rel`` in the clone.

    Uses ``git ls-files -- <rel>`` (empty stdout ⇒ nothing tracked).
    """
    result = _git(["ls-files", "--", rel], clone_dir)
    return bool(result.stdout.strip())


def _strip_transient_dirs(clone_dir: Path) -> None:
    """Delete kaizen-transient dirs from the clone — but ONLY untracked ones.

    The target repo may legitimately track files under ``.ai/`` (or even a
    checked-in ``__pycache__``-named path); deleting tracked files before
    ``git add -A`` would commit destructive DELETIONS of target-owned files
    into the kaizen PR. Tracked paths are left in place (with a stderr
    warning for ``.ai``). ``__pycache__`` / ``.pytest_cache`` are stripped
    RECURSIVELY (they nest under packages), skipping anything inside .git/.
    """
    ai_dir = clone_dir / ".ai"
    if ai_dir.exists():
        if _tracked_under(clone_dir, ".ai"):
            print(
                f"kaizen: warning — leaving {ai_dir} in place: the target repo "
                f"tracks files under .ai/ and kaizen must not commit their deletion",
                file=sys.stderr,
            )
        else:
            shutil.rmtree(ai_dir, ignore_errors=True)
    for name in ("__pycache__", ".pytest_cache"):
        # Materialize before deleting — rglob is lazy and we mutate the tree.
        for path in list(clone_dir.rglob(name)):
            rel_parts = path.relative_to(clone_dir).parts
            if ".git" in rel_parts:
                continue
            if not path.is_dir():
                continue
            if _tracked_under(clone_dir, path.relative_to(clone_dir).as_posix()):
                continue
            shutil.rmtree(path, ignore_errors=True)


def commit_cycle(
    clone_dir: Path,
    cycle_n: int,
    decisions: list[str],
    participants: list[str],
    n_tests: int,
    subject: str,
    minutes_rel_path: str,
    allow_empty: bool = False,
) -> None:
    """Stage all changes and produce the standard kaizen cycle commit.

    ``allow_empty`` (default False — team mode's behavior is unchanged) appends
    ``--allow-empty`` so the commit succeeds even when the working tree is clean.
    The HOST transport needs this: atelier's engine EAGER-MERGES each Phase-4
    writer's worktree into the clone's HEAD as it completes (``--no-ff`` merge
    commits with the engine's own identity), so by the time the host path
    commits the cycle there is nothing left UNCOMMITTED to stage. The kaizen
    cycle commit then stamps the standard cycle message (which the PR title/body
    render from) on top of the engine's merge commits. Team mode implementers
    write directly into the clone's working tree, so there ARE real staged
    changes and ``allow_empty`` stays False.
    """
    # Strip transient dirs so they never reach the PR diff (untracked only —
    # see _strip_transient_dirs for the tracked-file safety contract).
    _strip_transient_dirs(clone_dir)
    _git(["add", "-A"], clone_dir)
    summary = decisions[0] if decisions else "improvements applied"
    decisions_text = "\n".join(f"  {i + 1}. {d}" for i, d in enumerate(decisions))
    msg = (
        f"kaizen(cycle-{cycle_n}): {summary}\n\n"
        f"Meeting: {minutes_rel_path}\n"
        f"Participants: {', '.join(participants)}\n"
        f"Decisions:\n{decisions_text}\n"
        f"Tests: {n_tests} passed\n"
        f"Subject: {subject}"
    )
    commit_args = ["commit", "-m", msg]
    if allow_empty:
        commit_args.append("--allow-empty")
    _git(commit_args, clone_dir)


def commit_cycle_and_sha(
    clone_dir: Path,
    cycle_n: int,
    decisions: list[str],
    participants: list[str],
    n_tests: int,
    subject: str,
    minutes_rel_path: str,
    allow_empty: bool = False,
) -> str:
    """Produce the standard kaizen cycle commit and return its real commit SHA.

    Calls :func:`commit_cycle` (which stages + commits), then resolves
    ``git rev-parse HEAD`` (``check=False`` so a corrupt clone / unset HEAD
    surfaces the real exit code + stderr rather than a bare
    ``CalledProcessError`` with no message). Raises ``RuntimeError`` on a
    non-zero exit or an empty SHA.

    Lifted from the inline rev-parse dance in
    :func:`scripts.team_executor.team_cycle_executor` so host AND team modes
    commit + read back the SHA through one code path. ``allow_empty`` is passed
    straight through to :func:`commit_cycle` (the host transport sets it True —
    see that function's docstring for why the host tree is already committed).
    """
    commit_cycle(
        clone_dir=clone_dir,
        cycle_n=cycle_n,
        decisions=decisions,
        participants=participants,
        n_tests=n_tests,
        subject=subject,
        minutes_rel_path=minutes_rel_path,
        allow_empty=allow_empty,
    )
    # F13: check=False + explicit assert so the error names the actual exit
    # code and stderr (check=True would raise CalledProcessError with no
    # captured stdout/stderr, masking a corrupt clone / missing HEAD).
    rev = subprocess.run(
        ["git", "-C", str(clone_dir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if rev.returncode != 0:
        raise RuntimeError(
            f"git rev-parse HEAD in {clone_dir} exited "
            f"{rev.returncode}: {(rev.stderr or rev.stdout or '').strip()}"
        )
    commit_sha = rev.stdout.strip()
    if not commit_sha:
        raise RuntimeError(
            f"git rev-parse HEAD in {clone_dir} returned an empty SHA; "
            "the clone may be corrupt or HEAD may be unset."
        )
    return commit_sha


def push_branch(clone_dir: Path, branch: str) -> None:
    """Push branch to origin from the clone."""
    _git(["push", "origin", branch], clone_dir)


if __name__ == "__main__":
    print(
        "scripts/cycle_git.py is a library module — import its functions.",
        file=sys.stderr,
    )
    sys.exit(1)
