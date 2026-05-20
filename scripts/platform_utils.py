"""Platform detection and cross-platform filesystem helpers."""
from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path


def is_windows() -> bool:
    return sys.platform == "win32"


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def safe_rmtree(path: Path) -> None:
    """Recursively delete a directory tree.

    On Windows, git pack files in .git/objects/ are marked read-only,
    causing shutil.rmtree to fail with PermissionError. The handler
    clears the read-only attribute and retries. No-op when the path
    does not exist.
    """
    if not path.exists():
        return

    if sys.version_info >= (3, 12):
        def _on_exc(func, target, exc):
            os.chmod(target, stat.S_IWRITE)
            func(target)
        shutil.rmtree(path, onexc=_on_exc)
    else:
        def _on_error(func, target, exc_info):
            os.chmod(target, stat.S_IWRITE)
            func(target)
        shutil.rmtree(path, onerror=_on_error)
