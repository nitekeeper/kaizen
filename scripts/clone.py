"""Clone target repositories into the experiment area and tear them down."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from scripts.git_utils import git as _git
from scripts.platform_utils import safe_rmtree


# ── Public functions ───────────────────────────────────────────────────────

def get_remote_url(repo_dir: Path) -> str:
    """Return the origin remote URL of repo_dir."""
    result = _git(["remote", "get-url", "origin"], repo_dir)
    return result.stdout.strip()


def clone_repo(remote_url: str, dest: Path) -> None:
    """Clone remote_url into dest and configure a known git identity.

    Caller is responsible for providing remote_url directly; this function
    does not look up an origin from any other repo.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "-b", "main", remote_url, str(dest)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    _git(["config", "user.email", "kaizen@kaizen.local"], dest)
    _git(["config", "user.name", "Kaizen"], dest)


def cleanup_experiment(experiment_dir: Path) -> None:
    """Delete the experiment directory. Safe if it does not exist."""
    safe_rmtree(experiment_dir)


if __name__ == "__main__":
    # Usage:
    #   python3 scripts/clone.py clone <git-url> <dest>
    #   python3 scripts/clone.py cleanup <experiment-dir>
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""

    if cmd == "clone":
        if len(sys.argv) < 4:
            print("Usage: python3 scripts/clone.py clone <git-url> <dest>", file=sys.stderr)
            sys.exit(1)
        remote_url = sys.argv[2]
        dest = Path(sys.argv[3])
        clone_repo(remote_url, dest)
        print(f"CLONE_DIR={dest}")

    elif cmd == "cleanup":
        if len(sys.argv) < 3:
            print("Usage: python3 scripts/clone.py cleanup <experiment-dir>", file=sys.stderr)
            sys.exit(1)
        cleanup_experiment(Path(sys.argv[2]))
        print("experiment/ removed.")

    else:
        print("Commands: clone, cleanup", file=sys.stderr)
        sys.exit(1)
