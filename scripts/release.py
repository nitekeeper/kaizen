"""Build a dist/v<version>/ bundle for GitHub Release attachment.

Kaizen is NOT distributed via Agora (per CLAUDE.md: personal-use only). The
dist bundle exists solely as a GitHub Release artifact for direct download —
users who prefer a frozen snapshot over `git clone` can grab the zip from
the Releases page.

The bundle includes: .claude-plugin/ (the canonical manifest), scripts/,
skills/, internal/, migrations/, a manifest.json with file inventory, and
INSTALL.md instructions.

dist/ body is gitignored; only manifest tracking is committed.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

# Claude Code reads .claude-plugin/plugin.json (the canonical manifest); the
# dist bundle MUST include that directory for `claude --plugin-dir` to work.
# Kaizen-specific directory set: no `db/` (state lives in `.ai/memex.db`,
# rebuilt from migrations/) and no `prompts/` (Kaizen reuses Atelier's
# agent roster rather than vendoring prompts).
_INCLUDE_DIRS = [".claude-plugin", "scripts", "skills", "internal", "migrations"]
_INCLUDE_FILES = ["pyproject.toml", "requirements.txt", "README.md", "CLAUDE.md", "CHANGELOG.md"]


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(version: str, target_root: Path | str = "dist") -> Path:
    """Build a dist bundle. Returns the path to the version directory."""
    target_root = Path(target_root)
    version_dir = target_root / f"v{version}"
    if version_dir.exists():
        shutil.rmtree(version_dir)
    version_dir.mkdir(parents=True)

    repo_root = Path.cwd()
    files_manifest: list[dict] = []

    # Copy directories
    for dirname in _INCLUDE_DIRS:
        src = repo_root / dirname
        if not src.exists():
            continue
        dst = version_dir / dirname
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for f in dst.rglob("*"):
            if f.is_file():
                files_manifest.append(
                    {
                        "path": str(f.relative_to(version_dir)),
                        "sha256": _hash_file(f),
                        "bytes": f.stat().st_size,
                    }
                )

    # Copy individual files
    for fname in _INCLUDE_FILES:
        src = repo_root / fname
        if not src.exists():
            continue
        dst = version_dir / fname
        shutil.copy2(src, dst)
        files_manifest.append(
            {
                "path": fname,
                "sha256": _hash_file(dst),
                "bytes": dst.stat().st_size,
            }
        )

    # INSTALL.md (generated, not copied)
    install_md = f"""# Kaizen v{version} Install Instructions

Kaizen is a **personal-use** Claude Code plugin. It is NOT distributed via
Agora and is NOT published to PyPI. Install by cloning the repo (or
downloading this bundle) and registering it manually with Claude Code.

## Hard dependencies

Kaizen refuses to run if any of these are missing:

- `git` on PATH
- `gh` CLI on PATH and authenticated (`gh auth status` exits 0)
- Atelier installed via Agora (`atelier:run` skill available) — Kaizen
  depends on Atelier's 61-role roster + dev-arc skills for cycle agents
- Memex installed via Agora (`memex:run` skill available) — used by agents
  to capture abandonment reports and cycle minutes
- Python 3.11+

## Fresh install (clone)

1. Clone Kaizen as a sibling to atelier, memex, agora:
   ```
   git clone https://github.com/nitekeeper/kaizen.git
   cd kaizen
   ```
2. Install Python deps:
   ```
   pip install -r requirements.txt
   ```
3. Run the setup script (verifies dependencies and applies the schema
   migration to `.ai/memex.db`):
   ```
   PYTHONPATH=. python3 scripts/setup.py
   ```
4. Register Kaizen as a local plugin in Claude Code (manual step — Kaizen
   is not distributed via Agora).

## Fresh install (this bundle)

If you grabbed `kaizen-v{version}.zip` from the GitHub Release page:

1. Unzip to a sibling directory of atelier/memex/agora.
2. From the unzipped directory, run steps 2-4 above.

## Usage

Once registered, the only user-invocable command is:

```
/kaizen:improve <git-url> [--cycles N] [--subject "<area to improve>"]
```

All other operations live in `internal/<name>/SKILL.md` and are reached
by agents on demand.

## Verifying

After setup:
- `.ai/memex.db` exists in the Kaizen repo root
- `atelier:run` and `memex:run` skills resolve from your Claude Code env
- `gh auth status` exits 0

## Storage

| Path | Purpose | Tracked? |
|---|---|---|
| `~/.memex/` | Memex Brain — abandonment reports, cycle minutes, cross-repo learnings | No (lives outside repo) |
| `.ai/memex.db` | Kaizen's project/run/cycle/abandonment state | No (gitignored, rebuilt via migrations) |
| `experiment/<owner>-<repo>/` | Ephemeral clone of the current target repo | No (gitignored, deleted after PR opens) |

Kaizen never writes to the target repo's working tree outside the experiment
clone. The user's local copy of the target repo is never touched.
"""
    (version_dir / "INSTALL.md").write_text(install_md, encoding="utf-8")
    files_manifest.append(
        {
            "path": "INSTALL.md",
            "sha256": _hash_file(version_dir / "INSTALL.md"),
            "bytes": (version_dir / "INSTALL.md").stat().st_size,
        }
    )

    # Manifest
    manifest = {
        "version": version,
        "built_at": datetime.now(UTC).isoformat(),
        "file_count": len(files_manifest),
        "files": sorted(files_manifest, key=lambda f: f["path"]),
    }
    (version_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return version_dir


if __name__ == "__main__":
    import sys

    version = sys.argv[1] if len(sys.argv) > 1 else "0.1.0"
    out = build(version)
    print(f"Built: {out}")
