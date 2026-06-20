"""``KAIZEN_TRANSPORT`` selector — host (default) vs prose (M8 strangler-fig).

M8 replaces kaizen's SQLite queue-bridge with atelier v1.10.0's deterministic
host engine via a strangler-fig migration. This module is the SINGLE place the
transport is resolved, so the wiring lands behind one flag:

  * ``host`` (DEFAULT, unset/empty/whitespace) — the in-process atelier host
    engine, live-validated in M8b and now the default dispatch path. The Phase-4
    implementation-wave executor
    (:func:`scripts.host_executor.host_cycle_executor`) is wired + e2e-tested,
    and M8's glue PR connects it to the top-level ``kaizen:improve`` flow at the
    SUBAGENT-SKILL layer: the Phase 1-3 meeting produces the Action-Items DAG
    in-prose, then :mod:`scripts.host_cycle_entry` hands that DAG to the executor.
    :func:`require_wired_transport` is therefore a SCOPED guard (``allow_host``):
    it resolves ``host`` cleanly for the wired ``scripts.host_cycle_entry`` path,
    but still raises :class:`NotImplementedError` for the run.py Python
    ``cycle_executor`` slot, which has no host branch + no DAG source (M8c /
    Option-B territory). It deliberately does NOT silently fall back to
    ``prose`` — that would mask the flag.
  * ``prose`` — the in-prose, hand-orchestrated subagent cycle (the Phase 4 /
    5a / 5b / 5b' / 5c prose in ``internal/cycle/SKILL.md``). This is the
    EXPLICIT opt-out from the default host engine (set
    ``KAIZEN_TRANSPORT=prose``); it dispatches the cycle agents in-prose rather
    than through the in-process engine, and is kept as a documented alternative.

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
TRANSPORT_PROSE = "prose"
TRANSPORT_HOST = "host"
VALID_TRANSPORTS: frozenset[str] = frozenset({TRANSPORT_PROSE, TRANSPORT_HOST})


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
            f"valid values: {valid} (unset/empty defaults to {TRANSPORT_HOST!r})"
        )
        self.transport = transport


def resolve_transport(env: Mapping[str, str] | None = None) -> str:
    """Resolve the dispatch transport from ``KAIZEN_TRANSPORT``.

    Returns ``"host"`` when the var is unset / empty / whitespace (the M8c
    DEFAULT). Returns ``"prose"`` when explicitly set to ``prose`` (the
    in-prose opt-out). Any other value raises :class:`UnknownTransportError`.

    ``env`` defaults to ``os.environ``; pass an explicit mapping in tests.
    """
    if env is None:
        env = os.environ
    raw = (env.get(TRANSPORT_ENV_VAR) or "").strip()
    if not raw:
        return TRANSPORT_HOST
    if raw not in VALID_TRANSPORTS:
        raise UnknownTransportError(raw)
    return raw


def require_wired_transport(
    env: Mapping[str, str] | None = None,
    *,
    allow_host: bool = False,
) -> str:
    """Resolve the transport AND enforce that the caller's path is wired for it.

    M8 wires ``host`` at the SUBAGENT-SKILL layer: the Phase 1-3 meeting produces
    the Action-Items DAG in-prose, then :mod:`scripts.host_cycle_entry` hands that
    DAG to :func:`scripts.host_executor.host_cycle_executor`. That is the ONE wired
    host entrypoint. The run.py Python ``cycle_executor`` slot (mode=team)
    has NO host branch and NO orchestrator-side DAG source — selecting ``host``
    THERE would silently route into a path that cannot produce ``action_items``
    (the M8c / Option-B territory the glue PR deliberately defers).

    So the guard is SCOPED, not global (RISK-4):

      * ``allow_host=False`` (DEFAULT) — the run.py Python-cycle-executor contract:
        ``host`` raises :class:`NotImplementedError`. Any FUTURE run.py caller that
        forgets the M8c factoring fails loud instead of half-wiring a broken run.
      * ``allow_host=True`` — the :mod:`scripts.host_cycle_entry` contract: ``host``
        resolves cleanly (the DAG was produced upstream and is handed in).

    ``prose`` returns normally in both cases. Unknown values still raise
    :class:`UnknownTransportError` (via :func:`resolve_transport`). Centralizing the
    env semantics here keeps :data:`TRANSPORT_ENV_VAR` resolution in ONE place.
    """
    transport = resolve_transport(env)
    if transport == TRANSPORT_HOST and not allow_host:
        raise NotImplementedError(
            f"{TRANSPORT_ENV_VAR}={TRANSPORT_HOST}: the host engine is wired at the "
            f"subagent-SKILL layer via scripts.host_cycle_entry (which produces the "
            f"Action-Items DAG orchestrator-side and hands it to "
            f"host_cycle_executor). The run.py Python cycle-executor slot "
            f"(mode=team) has NO host branch and NO DAG source — routing "
            f"{TRANSPORT_HOST!r} there is M8c (Option-B) territory and is NOT yet "
            f"connected. Use the explicit {TRANSPORT_PROSE!r} transport here, or "
            f"invoke scripts.host_cycle_entry (which passes allow_host=True)."
        )
    return transport
