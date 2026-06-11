"""Shared numeric-semver resolution for Agora plugin-cache version directories.

The Agora plugin cache lays out installs as
``~/.claude/plugins/cache/agora/<plugin>/<version>/``. Picking "the newest"
version by lexicographic sort is WRONG (``'2.9.0' > '2.10.0'``); this module
provides the numeric comparison shared by
:func:`scripts.seed_atelier_in_clone.find_atelier_root` and
:func:`scripts.codegraph_recon.find_memex_root`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

# X.Y.Z prefix (tolerates a trailing pre-release/build suffix on the dir name).
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def parse_version(name: str) -> tuple[int, int, int] | None:
    """Parse an ``X.Y.Z`` prefix from a version-dir name; None if unparseable.

    Robust to non-semver names (returns None so the caller skips them) and to a
    trailing suffix (e.g. ``2.9.0-rc1`` → ``(2, 9, 0)``).
    """
    m = _VERSION_RE.match(name.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def newest_version_dir(
    cache_dir: Path,
    is_valid: Callable[[Path], bool],
    min_version: tuple[int, int, int] | None = None,
) -> Path | None:
    """Return the numerically-highest valid version dir under ``cache_dir``.

    Skips non-directories, names that don't parse as ``X.Y.Z``, versions below
    ``min_version`` (when given), and dirs for which ``is_valid`` is False.
    Returns ``None`` when no candidate qualifies (including when ``cache_dir``
    itself is missing).
    """
    if not cache_dir.is_dir():
        return None
    best: tuple[tuple[int, int, int], Path] | None = None
    for child in cache_dir.iterdir():
        if not child.is_dir():
            continue
        ver = parse_version(child.name)
        if ver is None:
            continue
        if min_version is not None and ver < min_version:
            continue
        if not is_valid(child):
            continue
        if best is None or ver > best[0]:
            best = (ver, child)
    return best[1] if best is not None else None
