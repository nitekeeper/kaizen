"""Seed atelier's full schema + role roster into a clone's `.ai/memex.db`.

Wraps subprocess invocations of atelier's `scripts/migrate.py` and
`scripts/seed_roles.py` against a cloned target's local DB.

Resolves atelier's location from the Agora plugin cache
(~/.claude/plugins/cache/agora/atelier/<version>/); see find_atelier_root().
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

try:
    from scripts.plugin_cache import newest_version_dir
except ImportError:  # standalone `python3 scripts/seed_atelier_in_clone.py ...`
    # (sys.path[0] is scripts/, so the sibling module imports flat).
    from plugin_cache import newest_version_dir

# ── Locate atelier ─────────────────────────────────────────────────────────

_AGORA_ATELIER = Path.home() / ".claude" / "plugins" / "cache" / "agora" / "atelier"
_ATELIER_MARKERS = ("scripts/migrate.py", "scripts/seed_roles.py")


def _looks_like_atelier(candidate: Path) -> bool:
    return all((candidate / m).exists() for m in _ATELIER_MARKERS)


def find_atelier_root() -> Path:
    """Resolve atelier's root from the Agora plugin cache.

    Picks the numerically-highest version directory under
    ~/.claude/plugins/cache/agora/atelier/ that contains the required markers
    (numeric semver compare via :func:`scripts.plugin_cache.newest_version_dir`
    — lexicographic sort would rank ``2.9.0`` above ``2.10.0``).
    Raises RuntimeError when not found.
    """
    if not _AGORA_ATELIER.is_dir():
        raise RuntimeError(
            f"Atelier plugin cache not found at {_AGORA_ATELIER}. "
            "Install Atelier via Agora before running Kaizen."
        )
    candidate = newest_version_dir(_AGORA_ATELIER, _looks_like_atelier)
    if candidate is not None:
        return candidate
    raise RuntimeError(
        f"No valid Atelier installation found in {_AGORA_ATELIER}. Reinstall Atelier via Agora."
    )


# ── Subprocess wrappers ────────────────────────────────────────────────────


def _atelier_env(atelier_root: Path) -> dict[str, str]:
    """Return a minimal environment dict for atelier subprocesses.

    Forwards only PATH, HOME, PYTHONPATH, and locale/temp-dir vars. Never
    forwards session tokens, API keys, or other ambient credentials — those
    have no business reaching subprocesses loaded from a plugin cache.
    """
    env: dict[str, str] = {}
    for key in ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TEMP", "TMP"):
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    env["PYTHONPATH"] = str(atelier_root)
    return env


def _copy_roles_agents_from_atelier(clone_dir: Path) -> None:
    """Copy roles and agents from atelier's Memex-resolved DB into the clone's local DB.

    Atelier's seed_roles.py always routes writes to ~/.memex/agents.db via its
    mode_detector, ignoring the db_path argument. This helper bridges the gap
    by copying the result rows into the clone's .ai/memex.db so the cycle
    agents can resolve participant profiles locally.
    """
    registry_path = Path.home() / ".memex" / "registry.json"
    if not registry_path.exists():
        raise RuntimeError(
            f"Memex registry not found at {registry_path}. "
            "Run Atelier setup before seeding a clone."
        )
    registry = json.loads(registry_path.read_text())
    try:
        agents_db_path = registry["agents"]["path"]
    except KeyError as exc:
        raise RuntimeError(f"Memex registry has no 'agents' store: {registry_path}") from exc

    dst_db = str(clone_dir / ".ai" / "memex.db")
    src_conn = sqlite3.connect(agents_db_path)
    dst_conn = sqlite3.connect(dst_db)
    try:
        roles = src_conn.execute(
            "SELECT id, name, description, created_at, updated_at FROM roles"
        ).fetchall()
        for row in roles:
            dst_conn.execute(
                "INSERT OR REPLACE INTO roles "
                "(id, name, description, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                row,
            )
        agents = src_conn.execute(
            "SELECT id, name, role_id, profile, created_at, updated_at FROM agents"
        ).fetchall()
        for row in agents:
            dst_conn.execute(
                "INSERT OR REPLACE INTO agents "
                "(id, name, role_id, profile, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                row,
            )
        dst_conn.commit()
    finally:
        src_conn.close()
        dst_conn.close()


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
    _copy_roles_agents_from_atelier(clone_dir)


def ensure_wiki_dir(clone_dir: Path) -> None:
    """Ensure <clone_dir>/.ai/wiki/ exists."""
    (clone_dir / ".ai" / "wiki").mkdir(parents=True, exist_ok=True)


def seed_all(clone_dir: Path) -> None:
    """Run the full seed sequence: schema → roles → wiki dir."""
    seed_atelier_schema(clone_dir)
    seed_atelier_roles(clone_dir)
    ensure_wiki_dir(clone_dir)


if __name__ == "__main__":
    # Usage: python3 scripts/seed_atelier_in_clone.py <clone-dir>
    if len(sys.argv) < 2:
        print(
            "Usage: python3 scripts/seed_atelier_in_clone.py <clone-dir>",
            file=sys.stderr,
        )
        sys.exit(1)
    seed_all(Path(sys.argv[1]))
    print("Atelier schema + roles seeded; .ai/wiki/ ensured.")
