"""``KAIZEN_TRANSPORT`` selector — bridge (default) vs host (M8 strangler-fig).

M8 replaces kaizen's SQLite queue-bridge with atelier v1.10.0's deterministic
host engine via a strangler-fig migration. This module is the SINGLE place the
transport is resolved, so the wiring lands behind one flag:

  * ``bridge`` (DEFAULT, unset/empty/whitespace) — the existing SQLite
    queue-bridge dispatch. Byte-for-byte unchanged.
  * ``host`` — the in-process atelier host engine. RECOGNIZED but NOT YET WIRED
    in this PR (M8a-1, foundation only): selecting it resolves cleanly and then
    raises :class:`NotImplementedError` at dispatch time. It deliberately does
    NOT silently fall back to ``bridge`` — that would mask the flag.

Any other value raises :class:`UnknownTransportError` (fail-loud, mirroring
atelier's ``scripts.dispatch.resolve_transport``): a typo or a stale value in
someone's shell must surface loudly, not silently select the default.

The resolver lives in its own module (rather than in ``scripts/run.py``) so the
orchestrator's run/CRUD path is untouched until M8a-2 wires the host path — the
least-invasive seam for a foundation PR whose default behavior must be unchanged.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

TRANSPORT_ENV_VAR = "KAIZEN_TRANSPORT"
TRANSPORT_BRIDGE = "bridge"
TRANSPORT_HOST = "host"
VALID_TRANSPORTS: frozenset[str] = frozenset({TRANSPORT_BRIDGE, TRANSPORT_HOST})


class UnknownTransportError(RuntimeError):
    """Raised when ``KAIZEN_TRANSPORT`` carries a value outside
    :data:`VALID_TRANSPORTS`.

    A configuration error to fix at source — a bad transport must fail loud, it
    must not silently select the default. Mirrors atelier's
    ``scripts.dispatch.UnknownTransportError``.
    """

    def __init__(self, transport: object) -> None:
        valid = ", ".join(sorted(VALID_TRANSPORTS))
        super().__init__(
            f"{TRANSPORT_ENV_VAR}={transport!r} is not a recognized transport; "
            f"valid values: {valid} (unset/empty defaults to {TRANSPORT_BRIDGE!r})"
        )
        self.transport = transport


def resolve_transport(env: Mapping[str, str] | None = None) -> str:
    """Resolve the dispatch transport from ``KAIZEN_TRANSPORT``.

    Returns ``"bridge"`` when the var is unset / empty / whitespace. Returns
    ``"host"`` when explicitly set to ``host``. Any other value raises
    :class:`UnknownTransportError`.

    ``env`` defaults to ``os.environ``; pass an explicit mapping in tests.
    """
    if env is None:
        env = os.environ
    raw = (env.get(TRANSPORT_ENV_VAR) or "").strip()
    if not raw:
        return TRANSPORT_BRIDGE
    if raw not in VALID_TRANSPORTS:
        raise UnknownTransportError(raw)
    return raw


def require_wired_transport(env: Mapping[str, str] | None = None) -> str:
    """Resolve the transport AND enforce that the selected one is wired.

    For M8a-1 (foundation) ``host`` is recognized but not yet wired: this raises
    a clear :class:`NotImplementedError` rather than silently falling back to
    ``bridge``. ``bridge`` returns normally. Unknown values still raise
    :class:`UnknownTransportError` (via :func:`resolve_transport`).

    Call this from the dispatch seam once M8a-2 wires the host path; until then
    it is the guard that keeps ``host`` from masquerading as ``bridge``.
    """
    transport = resolve_transport(env)
    if transport == TRANSPORT_HOST:
        raise NotImplementedError(
            f"{TRANSPORT_ENV_VAR}={TRANSPORT_HOST} not wired until M8a-2; "
            f"the deterministic host engine is recognized but its dispatch path "
            f"is not yet connected. Use the default {TRANSPORT_BRIDGE!r} transport."
        )
    return transport
