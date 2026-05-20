"""Kaizen cycle git operations — branch, commit, push.

Branch naming format:
    kaizen/<subject-slug-or-pm-directed>-YYYY-MM-DD-HHMM
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
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
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H%M")
    slug = _slugify(subject)
    branch = f"kaizen/{slug}-{date_str}-{time_str}"
    _git(["checkout", "-b", branch], clone_dir)
    return branch


def commit_cycle(
    clone_dir: Path,
    cycle_n: int,
    decisions: list[str],
    participants: list[str],
    n_tests: int,
    subject: str,
    minutes_rel_path: str,
) -> None:
    """Stage all changes and produce the standard kaizen cycle commit."""
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
    _git(["commit", "-m", msg], clone_dir)


def push_branch(clone_dir: Path, branch: str) -> None:
    """Push branch to origin from the clone."""
    _git(["push", "origin", branch], clone_dir)


if __name__ == "__main__":
    print(
        "scripts/cycle_git.py is a library module — import its functions.",
        file=sys.stderr,
    )
    sys.exit(1)
