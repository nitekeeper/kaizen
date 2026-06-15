"""``KAIZEN_TRANSPORT`` selector — bridge (default) vs host (M8 strangler-fig).

M8 replaces kaizen's SQLite queue-bridge with atelier v1.10.0's deterministic
host engine via a strangler-fig migration. This module is the SINGLE place the
transport is resolved, so the wiring lands behind one flag:

  * ``bridge`` (DEFAULT, unset/empty/whitespace) — the existing SQLite
    queue-bridge dispatch. Byte-for-byte unchanged.
  * ``host`` — the in-process atelier host engine. As of M8a-2a the Phase-4
    implementation-wave executor (:func:`scripts.host_executor.host_cycle_executor`)
    IS wired and e2e-tested as a unit, BUT the top-level ``kaizen:improve``
    meeting→executor integration (Phases 1-3 glue in ``run.py`` / the SKILL) is a
    LATER PR (M8a-2 follow-up). So selecting ``host`` resolves cleanly and the
    Phase-4 executor is fully reachable directly, but
    :func:`require_wired_transport` — the TOP-LEVEL orchestrator guard — still
    raises :class:`NotImplementedError` so a half-wired top-level command cannot be
    silently invoked in a broken state. It deliberately does NOT silently fall back
    to ``bridge`` — that would mask the flag.

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
    """Resolve the transport AND enforce that the TOP-LEVEL path is wired.

    M8a-2a wired the Phase-4 executor (:func:`scripts.host_executor.host_cycle_executor`),
    but NOT the top-level ``kaizen:improve`` meeting→executor integration (Phases
    1-3 glue is a follow-up PR). This is the TOP-LEVEL orchestrator guard: it still
    raises a clear :class:`NotImplementedError` for ``host`` rather than letting
    ``kaizen:improve`` invoke a half-wired command in a broken state. ``bridge``
    returns normally. Unknown values still raise :class:`UnknownTransportError`
    (via :func:`resolve_transport`).

    The Phase-4 executor is independently reachable + e2e-tested as a unit; this
    guard only protects the orchestrator entrypoint until the meeting glue lands.
    Drop the ``host`` branch here when that follow-up PR wires the integration.
    """
    transport = resolve_transport(env)
    if transport == TRANSPORT_HOST:
        raise NotImplementedError(
            f"{TRANSPORT_ENV_VAR}={TRANSPORT_HOST}: the Phase-4 host executor "
            f"(scripts.host_executor.host_cycle_executor) is wired + e2e-tested, "
            f"but the top-level kaizen:improve meeting->executor integration is a "
            f"M8a-2 follow-up and is NOT yet connected. The orchestrator must not "
            f"invoke a half-wired command. Use the default {TRANSPORT_BRIDGE!r} "
            f"transport, or call host_cycle_executor directly."
        )
    return transport
