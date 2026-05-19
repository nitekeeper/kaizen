"""Seed atelier's full schema + role roster into a clone's `.ai/memex.db`.

Wraps subprocess invocations of atelier's `scripts/migrate.py` and
`scripts/seed_roles.py` against a cloned target's local DB.

Resolves atelier's location from the Agora plugin cache
(~/.claude/plugins/cache/agora/atelier/<version>/); see find_atelier_root().
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


# ── Locate atelier ─────────────────────────────────────────────────────────

_AGORA_ATELIER = Path.home() / ".claude" / "plugins" / "cache" / "agora" / "atelier"
_ATELIER_MARKERS = ("scripts/migrate.py", "scripts/seed_roles.py")


def _looks_like_atelier(candidate: Path) -> bool:
    return all((candidate / m).exists() for m in _ATELIER_MARKERS)


def find_atelier_root() -> Path:
    """Resolve atelier's root from the Agora plugin cache.

    Picks the highest-sorted version directory under
    ~/.claude/plugins/cache/agora/atelier/ that contains the required markers.
    Raises RuntimeError when not found.
    """
    if not _AGORA_ATELIER.is_dir():
        raise RuntimeError(
            f"Atelier plugin cache not found at {_AGORA_ATELIER}. "
            "Install Atelier via Agora before running Kaizen."
        )
    versions = sorted(
        (d for d in _AGORA_ATELIER.iterdir() if d.is_dir()),
        key=lambda p: p.name,
        reverse=True,
    )
    for candidate in versions:
        if _looks_like_atelier(candidate):
            return candidate
    raise RuntimeError(
        f"No valid Atelier installation found in {_AGORA_ATELIER}. "
        "Reinstall Atelier via Agora."
    )


# ── Subprocess wrappers ────────────────────────────────────────────────────

def _atelier_env(atelier_root: Path) -> dict:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(atelier_root)
    return env


def seed_atelier_schema(clone_dir: Path) -> None:
    """Apply atelier's migrations to <clone_dir>/.ai/memex.db via subprocess."""
    atelier_root = find_atelier_root()
    db_path = clone_dir / ".ai" / "memex.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    migrate_script = atelier_root / "scripts" / "migrate.py"
    result = subprocess.run(
        [sys.executable, str(migrate_script), str(db_path)],
        env=_atelier_env(atelier_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"atelier migrate.py failed (exit {result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


def seed_atelier_roles(clone_dir: Path) -> None:
    """Run atelier's seed_roles.py against <clone_dir>/.ai/memex.db via subprocess."""
    atelier_root = find_atelier_root()
    db_path = clone_dir / ".ai" / "memex.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    seed_script = atelier_root / "scripts" / "seed_roles.py"
    result = subprocess.run(
        [sys.executable, str(seed_script), str(db_path)],
        env=_atelier_env(atelier_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"atelier seed_roles.py failed (exit {result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


def ensure_wiki_dir(clone_dir: Path) -> None:
    """Ensure <clone_dir>/.ai/wiki/ exists."""
    (clone_dir / ".ai" / "wiki").mkdir(parents=True, exist_ok=True)


def seed_all(clone_dir: Path) -> None:
    """Run the full seed sequence: schema → roles → wiki dir."""
    seed_atelier_schema(clone_dir)
    seed_atelier_roles(clone_dir)
    ensure_wiki_dir(clone_dir)


if __name__ == "__main__":
    # Usage: python scripts/seed_atelier_in_clone.py <clone-dir>
    if len(sys.argv) < 2:
        print(
            "Usage: python scripts/seed_atelier_in_clone.py <clone-dir>",
            file=sys.stderr,
        )
        sys.exit(1)
    seed_all(Path(sys.argv[1]))
    print("Atelier schema + roles seeded; .ai/wiki/ ensured.")
