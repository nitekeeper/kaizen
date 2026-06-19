"""Tests for scripts/atelier_engine.py + scripts/transport.py (M8a-1 foundation).

The PR rests on these proofs:

  1. Import + restore — inside the swap window ``scripts.host_scheduler``
     resolves to ATELIER's tree; on exit kaizen's own ``scripts.*`` module
     objects are restored by identity (``id()`` match) and no atelier
     ``scripts.*`` leaks into ``sys.modules``.
  2. Lazy resolution — an atelier-ONLY module (``scripts.run_mode``) imported
     inside the window resolves under the atelier root.
  3. Hazard-as-a-test — a kaizen-ONLY module (``scripts.db``) imported inside
     the window resolves to atelier-or-absent, NEVER kaizen's. Encodes the
     documented closure/re-import hazard so a future refactor that breaks the
     contract is caught.
  4. Lock / non-reentrancy — the swap lock exists and the manager is not safely
     reentrant.
  5. Capability + version guard — a fake atelier root below the min version, or
     one missing the host-pipeline capability, raises ``EngineUnavailableError``.
  6. Transport flag — default → host; explicit ``bridge`` → resolves; the scoped
     wired guard (``require_wired_transport``) allows host only for the
     host_cycle_entry contract (``allow_host=True``) and still raises
     ``NotImplementedError`` for the run.py cycle-executor slot
     (``allow_host=False``); unknown → UnknownTransportError.

The atelier-touching tests skip cleanly when atelier is not installed (the
``_atelier_root`` fixture), so CI without an atelier cache still passes; the
maintainer's environment has atelier 1.10.0 and exercises them for real.
NO real ``claude`` CLI is invoked anywhere in this file.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from scripts import atelier_engine, transport
from scripts.atelier_engine import (
    _SWAP_LOCK,
    MIN_ATELIER_VERSION,
    EngineUnavailableError,
    assert_engine_available,
)

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def _atelier_root() -> Path:
    """Resolve the real atelier root, or skip the test if absent."""
    try:
        from scripts.seed_atelier_in_clone import find_atelier_root

        return find_atelier_root()
    except RuntimeError as exc:  # pragma: no cover - env-dependent
        pytest.skip(f"atelier not installed: {exc}")


# ── 1. Import + restore ─────────────────────────────────────────────────────


def test_window_yields_atelier_host_scheduler(_atelier_root):
    """Inside the window, host_scheduler.__file__ is under the atelier root."""
    root = str(_atelier_root.resolve())
    with atelier_engine.atelier_engine(_atelier_root) as host:
        assert host.__name__ == "scripts.host_scheduler"
        assert Path(host.__file__).resolve().is_relative_to(root)


def test_kaizen_scripts_module_restored_by_identity():
    """A kaizen-only scripts.* module is the SAME object before/after the window.

    Uses ``scripts.cycle`` (exists in kaizen, NOT in atelier) so the identity
    proof cannot be confounded by a same-named atelier module.
    """
    before = importlib.import_module("scripts.cycle")
    before_id = id(before)
    try:
        from scripts.seed_atelier_in_clone import find_atelier_root

        root = find_atelier_root()
    except RuntimeError as exc:  # pragma: no cover - env-dependent
        pytest.skip(f"atelier not installed: {exc}")

    with atelier_engine.atelier_engine(root):
        pass

    after = importlib.import_module("scripts.cycle")
    assert id(after) == before_id
    # Sanity: still kaizen's module, not atelier's tree.
    assert Path(after.__file__).resolve().is_relative_to(Path(__file__).resolve().parent.parent)


def test_no_atelier_scripts_leak_after_exit(_atelier_root):
    """After the window, no atelier-only scripts.* module lingers in sys.modules."""
    with atelier_engine.atelier_engine(_atelier_root):
        # Force an atelier-only module to load inside the window.
        importlib.import_module("scripts.run_mode")
        assert "scripts.run_mode" in sys.modules
    # run_mode is atelier-only → it must be gone after restore (kaizen has none).
    assert "scripts.run_mode" not in sys.modules
    # The atelier root must be removed from sys.path.
    assert str(_atelier_root.resolve()) not in sys.path


def test_exception_inside_window_still_restores(_atelier_root):
    """A raise inside the window must still restore ALL post-conditions.

    The `finally` clause is the only thing that restores state on the exception
    path; without it the guard tests stay green in isolation while a broken
    finally only surfaces as a fragile cross-test cascade
    (``ModuleNotFoundError: scripts.team_executor``). This test makes the
    exception-path restore a first-class assertion. Also closes N1
    (lock-release-on-exception): the `_SWAP_LOCK` assertion below proves the
    lock is released even when the body raises.
    """
    before = importlib.import_module("scripts.cycle")
    bid = id(before)
    with pytest.raises(ValueError), atelier_engine.atelier_engine(_atelier_root):
        # Touch an atelier-only module so a broken restore would leak it.
        importlib.import_module("scripts.run_mode")
        raise ValueError("boom")
    # (a) kaizen module restored by identity
    assert id(importlib.import_module("scripts.cycle")) == bid
    # (b) no atelier-only module leaked
    assert "scripts.run_mode" not in sys.modules
    # (c) atelier root dropped from sys.path
    assert str(_atelier_root.resolve()) not in sys.path
    # (d) lock released (closes N1)
    assert atelier_engine._SWAP_LOCK.acquire(blocking=False) is True
    atelier_engine._SWAP_LOCK.release()


# ── 2. Lazy resolution ──────────────────────────────────────────────────────


def test_lazy_import_resolves_to_atelier_tree(_atelier_root):
    """An atelier-only module imported lazily inside the window is atelier's."""
    root = str(_atelier_root.resolve())
    with atelier_engine.atelier_engine(_atelier_root):
        import scripts.run_mode as run_mode

        assert Path(run_mode.__file__).resolve().is_relative_to(root)


# ── 3. Hazard-as-a-test ─────────────────────────────────────────────────────


def test_kaizen_only_module_is_atelier_or_absent_inside_window(_atelier_root):
    """scripts.db (kaizen-only) inside the window is atelier's-or-absent, never kaizen's.

    Encodes the documented closure/re-import hazard: a callback that lazily does
    ``import scripts.db`` inside the window does NOT get kaizen's db module.
    Atelier 1.10.0 has no ``scripts/db.py``, so the import must FAIL here.
    """
    kaizen_root = Path(__file__).resolve().parent.parent
    with atelier_engine.atelier_engine(_atelier_root):
        try:
            mod = importlib.import_module("scripts.db")
        except ImportError:
            return  # absent inside window — hazard holds
        # If some future atelier ships scripts/db.py, it must NOT be kaizen's.
        assert not Path(mod.__file__).resolve().is_relative_to(kaizen_root)


# ── 4. Lock / non-reentrancy ────────────────────────────────────────────────


def test_swap_lock_exists_and_is_a_lock():
    assert _SWAP_LOCK is atelier_engine._SWAP_LOCK
    # A threading.Lock exposes acquire/release and is not held at rest.
    assert _SWAP_LOCK.acquire(blocking=False) is True
    _SWAP_LOCK.release()


def test_manager_is_not_safely_reentrant(_atelier_root):
    """Nesting the manager would deadlock — assert the lock is held inside.

    We do NOT actually nest (that would hang the test); instead we prove the
    lock is held across the window, which is exactly why nesting is unsafe.
    """
    with atelier_engine.atelier_engine(_atelier_root):
        # The window holds _SWAP_LOCK; a second acquire (what a nested call
        # would do) must fail without blocking.
        assert _SWAP_LOCK.acquire(blocking=False) is False
    # Released after exit.
    assert _SWAP_LOCK.acquire(blocking=False) is True
    _SWAP_LOCK.release()


# ── 5. Capability + version guard ───────────────────────────────────────────


def _make_fake_atelier(root: Path, version: str, *, with_capability: bool) -> None:
    """Build a minimal fake atelier tree under ``root``.

    Includes the seed markers (so find_atelier_root-style checks pass), a
    plugin.json with ``version``, and a scripts/host_scheduler.py that does or
    does not expose ``run_host_pipeline_for_project`` per ``with_capability``.
    """
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        f'{{"name": "atelier", "version": "{version}"}}', encoding="utf-8"
    )
    scripts_dir = root / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "__init__.py").write_text("", encoding="utf-8")
    cap = (
        "def run_host_pipeline_for_project():\n    return None\n"
        if with_capability
        else "# no host pipeline here\n"
    )
    (scripts_dir / "host_scheduler.py").write_text(cap, encoding="utf-8")
    (scripts_dir / "cli_dispatch.py").write_text("", encoding="utf-8")


def test_version_below_minimum_raises(tmp_path):
    fake = tmp_path / "atelier_old"
    _make_fake_atelier(fake, "1.9.0", with_capability=True)
    with pytest.raises(EngineUnavailableError, match="below the minimum"):
        assert_engine_available(fake)


def test_at_minimum_version_with_capability_passes(tmp_path):
    fake = tmp_path / "atelier_ok"
    want = ".".join(str(p) for p in MIN_ATELIER_VERSION)
    _make_fake_atelier(fake, want, with_capability=True)
    resolved = assert_engine_available(fake)
    assert resolved == fake.resolve()


def test_missing_capability_raises(tmp_path):
    fake = tmp_path / "atelier_nocap"
    want = ".".join(str(p) for p in MIN_ATELIER_VERSION)
    _make_fake_atelier(fake, want, with_capability=False)
    with pytest.raises(EngineUnavailableError, match="missing the required"):
        assert_engine_available(fake)


def test_unparseable_version_raises(tmp_path):
    fake = tmp_path / "atelier_bad"
    _make_fake_atelier(fake, "not-a-version", with_capability=True)
    with pytest.raises(EngineUnavailableError, match="parseable atelier version"):
        assert_engine_available(fake)


def test_fake_atelier_swap_restores_kaizen_modules(tmp_path):
    """The fake-root capability probe must also restore kaizen's scripts.*.

    Belt-and-braces: assert_engine_available swaps in the fake tree; afterward
    kaizen's own module identity is intact and the fake root is off sys.path.
    """
    fake = tmp_path / "atelier_restore"
    want = ".".join(str(p) for p in MIN_ATELIER_VERSION)
    _make_fake_atelier(fake, want, with_capability=True)

    before = importlib.import_module("scripts.cycle")
    assert_engine_available(fake)
    after = importlib.import_module("scripts.cycle")
    assert id(after) == id(before)
    assert str(fake.resolve()) not in sys.path


# ── 6. Transport flag ───────────────────────────────────────────────────────


def test_transport_default_is_host():
    # M8c: unset/empty now defaults to host (was bridge).
    assert transport.resolve_transport({}) == transport.TRANSPORT_HOST


def test_transport_empty_and_whitespace_default_to_host():
    # M8c: empty/whitespace now defaults to host (was bridge).
    assert transport.resolve_transport({"KAIZEN_TRANSPORT": ""}) == "host"
    assert transport.resolve_transport({"KAIZEN_TRANSPORT": "   "}) == "host"


def test_transport_bridge_resolves():
    # bridge is still reachable as the explicit opt-out.
    assert transport.resolve_transport({"KAIZEN_TRANSPORT": "bridge"}) == "bridge"


def test_transport_host_resolves():
    assert transport.resolve_transport({"KAIZEN_TRANSPORT": "host"}) == "host"


def test_transport_unknown_raises():
    with pytest.raises(transport.UnknownTransportError, match="not a recognized"):
        transport.resolve_transport({"KAIZEN_TRANSPORT": "bogus"})


def test_require_wired_explicit_bridge_ok():
    # The explicit bridge opt-out resolves cleanly in both allow_host modes.
    assert transport.require_wired_transport({"KAIZEN_TRANSPORT": "bridge"}) == "bridge"


def test_require_wired_default_host_not_implemented():
    """M8c: the default (unset) is now host. The run.py Python-cycle-executor
    contract (allow_host defaults False) STILL raises for host: that slot has no
    host branch + no DAG source (M8c / Option-B territory). The relaxation is
    scoped to scripts.host_cycle_entry, not global (M8 glue, RISK-4) — must NOT
    fall back to bridge, must NOT silently run half-wired."""
    with pytest.raises(NotImplementedError, match="host_cycle_entry"):
        transport.require_wired_transport({})


def test_require_wired_host_not_implemented():
    """Explicit host with allow_host=False still raises (same contract as the
    new default)."""
    with pytest.raises(NotImplementedError, match="host_cycle_entry"):
        transport.require_wired_transport({"KAIZEN_TRANSPORT": "host"})


def test_require_wired_host_allowed_for_entry():
    """The scripts.host_cycle_entry contract (allow_host=True) resolves host cleanly
    — the wired host entrypoint (M8 glue)."""
    assert (
        transport.require_wired_transport({"KAIZEN_TRANSPORT": "host"}, allow_host=True) == "host"
    )


def test_require_wired_default_allowed_resolves_host():
    """allow_host=True with an unset env resolves the new default (host) cleanly —
    the scripts.host_cycle_entry contract on the default path."""
    assert transport.require_wired_transport({}, allow_host=True) == "host"


def test_require_wired_unknown_still_raises():
    with pytest.raises(transport.UnknownTransportError):
        transport.require_wired_transport({"KAIZEN_TRANSPORT": "nope"})


def test_transport_reads_real_environ_default(monkeypatch):
    """With nothing passed, the resolver reads os.environ and defaults to host (M8c)."""
    monkeypatch.delenv("KAIZEN_TRANSPORT", raising=False)
    assert transport.resolve_transport() == "host"
