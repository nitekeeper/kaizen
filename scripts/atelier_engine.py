"""In-process import bridge to atelier v1.10.0's deterministic host engine.

This is the M8 strangler-fig FOUNDATION: it lets kaizen import and call
atelier's ``scripts.host_scheduler`` (the deterministic-host pipeline that
replaced the SQLite dispatch queue in atelier M7) *in-process*, without a
subprocess hop, by temporarily re-pointing the top-level ``scripts`` package
name at atelier's source tree.

Both kaizen and atelier ship a top-level ``scripts`` package. Python's import
system keys modules by name in ``sys.modules``, so the two trees cannot be
imported simultaneously under the same name. :func:`atelier_engine` resolves
this with a serialized, reversible swap:

  1. acquire :data:`_SWAP_LOCK` (the swap mutates process-global state),
  2. pop every ``scripts`` / ``scripts.*`` module out of ``sys.modules`` and
     snapshot it,
  3. prepend atelier's root to ``sys.path`` so ``import scripts.*`` resolves
     to atelier,
  4. import atelier's ``host_scheduler`` + ``cli_dispatch`` and yield,
  5. on exit, purge atelier's ``scripts.*`` and restore kaizen's snapshot
     byte-for-byte, then drop atelier's ``sys.path`` entry.

═══════════════════════════════════════════════════════════════════════════
CLOSURE / RE-IMPORT HAZARD — read before passing callbacks into the engine
═══════════════════════════════════════════════════════════════════════════
Inside the ``with atelier_engine(...) as host:`` window the name ``scripts``
resolves to ATELIER's package. Any kaizen code that runs inside the window and
performs ``import scripts.db`` (or any other kaizen-only ``scripts.*`` module)
will get atelier's module of that name — or an ``ImportError`` if atelier has
no such module. Kaizen-only modules such as ``scripts.db`` are NOT importable
as kaizen inside the window.

Therefore: callbacks handed to the engine MUST use **pre-bound references**
captured BEFORE entering the window (e.g. ``from scripts.db import
get_connection`` at module top, then pass ``get_connection``). Do NOT pass a
callback whose body does a lazy ``import scripts.<kaizen_module>`` — it will
silently resolve against atelier inside the window.

CONTRACT:
  * NOT re-entrant. The context manager holds :data:`_SWAP_LOCK` for the whole
    window; a nested ``atelier_engine(...)`` call from the same thread would
    deadlock. Never nest.
  * Single swap at a time across threads (the lock serializes concurrent
    callers).
  * The window must not run kaizen code that re-imports kaizen ``scripts.*``;
    pass already-bound callables only (see the hazard note above).

NOTE on the lock's scope: :data:`_SWAP_LOCK` serializes the *swap* (only one
``atelier_engine`` window mutates ``sys.modules`` at a time); it does NOT
prevent an already-running, unrelated kaizen thread from doing
``import scripts.db`` *during* the window — all threads share the one global
``sys.modules``, so any ``scripts.*`` import on any thread inside the window
resolves against atelier. The lock is a swap-serializer, not a global "no
kaizen ``scripts.*`` import" guard. (Relevant once M8a-2 wires concurrent
dispatch.)
"""

from __future__ import annotations

import contextlib
import importlib
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

from scripts.plugin_cache import parse_version
from scripts.seed_atelier_in_clone import find_atelier_root

# Minimum atelier version carrying the deterministic-host pipeline
# (``run_host_pipeline_for_project``). Below this the host engine does not
# exist and the prose transport must be used instead.
MIN_ATELIER_VERSION: tuple[int, int, int] = (1, 10, 0)

# The capability symbol the host pipeline exposes. Presence is required in
# addition to the version gate (belt-and-braces against a renamed/removed API).
_REQUIRED_CAPABILITY = "run_host_pipeline_for_project"

# The swap mutates the process-global ``sys.modules`` / ``sys.path``; serialize
# it. Held for the entire ``atelier_engine`` window, so the manager is NOT
# re-entrant — never nest.
_SWAP_LOCK = threading.Lock()


class EngineUnavailableError(RuntimeError):
    """Raised when atelier's deterministic host engine cannot be used.

    Carries an actionable message: either the resolved atelier version is below
    :data:`MIN_ATELIER_VERSION`, the install could not be located, or the
    imported ``host_scheduler`` is missing the required capability symbol. A
    configuration error to fix at source — not a worker outcome to absorb.
    """


def _purge_scripts_modules() -> dict:
    """Pop every ``scripts`` / ``scripts.*`` entry out of ``sys.modules``.

    Returns a snapshot dict (name -> module) so the caller can restore it
    verbatim. Mutating ``sys.modules`` while iterating it is unsafe, hence the
    ``list(...)`` copy of the keys.
    """
    snap = {}
    for name in list(sys.modules):
        if name == "scripts" or name.startswith("scripts."):
            snap[name] = sys.modules.pop(name)
    return snap


def _read_plugin_version(atelier_root: Path) -> tuple[int, int, int] | None:
    """Read ``.claude-plugin/plugin.json`` version as an ``(X, Y, Z)`` tuple.

    Returns ``None`` if the file is missing/unreadable or the version string is
    unparseable (the caller treats ``None`` as "version gate failed").
    """
    import json

    plugin_json = atelier_root / ".claude-plugin" / "plugin.json"
    try:
        data = json.loads(plugin_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return parse_version(str(data.get("version", "")))


def assert_engine_available(atelier_root=None) -> Path:
    """Verify atelier's deterministic host engine is usable; return its root.

    Performs the version gate (``>= MIN_ATELIER_VERSION``, numeric semver
    compare via :func:`scripts.plugin_cache.parse_version` — never lexicographic)
    AND a capability probe (``hasattr(host_scheduler, "run_host_pipeline_for_project")``).
    Raises :class:`EngineUnavailableError` with an actionable message if either
    check fails.

    ``atelier_root`` defaults to :func:`find_atelier_root` (the canonical
    Agora-cache resolver). Passing an explicit root is for tests that point at a
    fixture tree — production callers should let it default.

    Returns the resolved, absolute atelier root :class:`Path`.
    """
    if atelier_root is None:
        atelier_root = find_atelier_root()
    atelier_root = Path(atelier_root).resolve()

    version = _read_plugin_version(atelier_root)
    if version is None:
        raise EngineUnavailableError(
            f"Could not read a parseable atelier version from "
            f"{atelier_root / '.claude-plugin' / 'plugin.json'}. "
            f"Reinstall atelier via Agora."
        )
    if version < MIN_ATELIER_VERSION:
        want = ".".join(str(p) for p in MIN_ATELIER_VERSION)
        have = ".".join(str(p) for p in version)
        raise EngineUnavailableError(
            f"atelier {have} at {atelier_root} is below the minimum {want} "
            f"required for the deterministic host engine. Upgrade atelier via "
            f"Agora (the host pipeline landed in {want})."
        )

    # Capability probe — import host_scheduler inside a swap window and check
    # the symbol. The window restores kaizen's scripts.* on exit.
    with atelier_engine(atelier_root) as host_scheduler:
        if not hasattr(host_scheduler, _REQUIRED_CAPABILITY):
            raise EngineUnavailableError(
                f"atelier at {atelier_root} imported but host_scheduler is "
                f"missing the required {_REQUIRED_CAPABILITY!r} capability. "
                f"The installed atelier does not expose the deterministic host "
                f"pipeline; reinstall a build that does."
            )
    return atelier_root


@contextlib.contextmanager
def atelier_engine(atelier_root=None) -> Iterator[object]:
    """Yield atelier's ``host_scheduler`` module with ``scripts`` re-pointed.

    Inside the with-block, the name ``scripts`` resolves to ATELIER's package;
    on exit kaizen's ``scripts.*`` is restored byte-for-byte. Yields atelier's
    ``host_scheduler`` module. NOT re-entrant (holds :data:`_SWAP_LOCK` for the
    whole window). The window MUST NOT run kaizen code that re-imports kaizen
    ``scripts.*`` — pass already-bound callables only (see the module docstring's
    closure / re-import hazard section).

    ``atelier_root`` defaults to :func:`find_atelier_root`. Note this does NOT
    run the version/capability gate — call :func:`assert_engine_available`
    first when you need that guarantee. (``assert_engine_available`` itself
    uses this manager for its capability probe, so the manager stays gate-free
    to avoid recursion.)
    """
    if atelier_root is None:
        atelier_root = find_atelier_root()
    atelier_root = str(Path(atelier_root).resolve())
    with _SWAP_LOCK:
        kaizen_snap = _purge_scripts_modules()
        sys.path.insert(0, atelier_root)
        try:
            host_scheduler = importlib.import_module("scripts.host_scheduler")
            importlib.import_module("scripts.cli_dispatch")
            yield host_scheduler
        finally:
            _purge_scripts_modules()
            sys.modules.update(kaizen_snap)
            with contextlib.suppress(ValueError):
                sys.path.remove(atelier_root)
