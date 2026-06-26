"""Canonical shared types for kaizen's tokenmeter (the FOUNDATION module).

Every other tokenmeter module (Seam B :mod:`scripts.tokenmeter_transcript`, Seam A
:mod:`scripts.tokenmeter_result`, pricing :mod:`scripts.tokenmeter_pricing`, static
:mod:`scripts.tokenmeter_static`, assembly :mod:`scripts.tokenmeter_schema`, and the
renderers :mod:`scripts.tokenmeter_render`) agrees on the handful of types defined
here. Each of those modules carries a ``try: from scripts.tokenmeter_model import …
except ImportError: <local mirror>`` so it stays importable on its own; once THIS
module exists the import resolves and those ``except`` mirrors become dead branches.
The types below are deliberately a SUPERSET of every mirror so the real types win
transparently — same field names, order, and defaults, plus a few extra helpers the
mirrors never needed.

Design invariants (kaizen token-usage benchmark spec §4):

* **FOUR token categories, never one total.** Claude bills ``input_tokens`` /
  ``output_tokens`` / ``cache_creation_input_tokens`` / ``cache_read_input_tokens``
  (reasoning is folded into ``output``). :class:`TokenUsage` carries exactly these
  four and DELIBERATELY exposes NO ``total()`` — ``cache_read`` routinely dwarfs the
  rest (~99% of token COUNT) while ``output`` dominates COST (~15x), so a single
  summed "total" is a lie. Cost (USD) is the only legitimate cross-category scalar
  and lives in :mod:`scripts.tokenmeter_pricing`, not here.
* **Saturating arithmetic.** ``TokenUsage.__add__`` clamps each field at
  :data:`SATURATION_MAX` so a pathological transcript can never overflow a rollup.
* **MAX merge for streaming partials.** ``TokenUsage.max_merge`` takes the per-field
  maximum — the rule Seam B uses to collapse out-of-order streaming partials that
  share a dedup key (the final partial holds the true counts).

Stdlib-only; frozen dataclasses (matching the seam mirrors), structural Protocol.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

#: Saturating ceiling for token sums — signed-64-bit max, far above any real
#: workload. Mirrors :data:`scripts.tokenmeter_schema.SATURATION_MAX` so the model
#: layer and the rollup layer saturate identically.
SATURATION_MAX = 2**63 - 1


def _saturate(value: int) -> int:
    """Clamp a non-negative running sum at :data:`SATURATION_MAX`."""
    return value if value < SATURATION_MAX else SATURATION_MAX


@dataclass(frozen=True)
class TokenUsage:
    """The four Claude-native billable token categories — NEVER summed into one.

    Field names / order / defaults MATCH the local mirrors in
    :mod:`scripts.tokenmeter_transcript` and :mod:`scripts.tokenmeter_result` exactly,
    so this canonical type is a drop-in replacement for them. Frozen (immutable) so a
    record's counts cannot be mutated after parsing.

    There is intentionally NO ``total()`` method — see the module docstring. Cost is
    the only valid cross-category scalar and it lives in the pricing module.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        """Per-field SATURATING sum of two usages (used by rollups)."""
        if not isinstance(other, TokenUsage):
            return NotImplemented
        return TokenUsage(
            input_tokens=_saturate(self.input_tokens + other.input_tokens),
            output_tokens=_saturate(self.output_tokens + other.output_tokens),
            cache_creation_input_tokens=_saturate(
                self.cache_creation_input_tokens + other.cache_creation_input_tokens
            ),
            cache_read_input_tokens=_saturate(
                self.cache_read_input_tokens + other.cache_read_input_tokens
            ),
        )

    def max_merge(self, other: TokenUsage) -> TokenUsage:
        """Per-field MAX of two usages.

        The rule Seam B uses to collapse out-of-order streaming partials that share a
        dedup key: the final partial holds the true per-field counts, so the maximum
        of each field is the complete value (never the sum — that double-counts).
        """
        return TokenUsage(
            input_tokens=max(self.input_tokens, other.input_tokens),
            output_tokens=max(self.output_tokens, other.output_tokens),
            cache_creation_input_tokens=max(
                self.cache_creation_input_tokens, other.cache_creation_input_tokens
            ),
            cache_read_input_tokens=max(
                self.cache_read_input_tokens, other.cache_read_input_tokens
            ),
        )


@dataclass(frozen=True)
class UsageRecord:
    """One assistant-turn usage record — the unit Seam B emits and the schema layer
    aggregates.

    Field names / order / defaults MATCH the local mirror in
    :mod:`scripts.tokenmeter_transcript` exactly. ``source`` is the provenance of the
    record (Seam B fills it with the transcript file path, NOT a measured/approximated
    tag), so it accepts any string and is never validated against a fixed vocabulary.
    Frozen (immutable).

    The trailing optional fields below are the descriptors the schema / pricing /
    render / rollup layers expect to find on a record; Seam B fills the ones the
    transcript carries (``model``, ``timestamp`` + parsed ``ts_epoch_ms``, and the
    cache-write TTL split ``cache_creation_5m`` / ``cache_creation_1h``), while
    ``run`` / ``phase`` are left ``None`` for Cycle-2 integration to tag. They are
    NOT a fifth token category — the four-category :class:`TokenUsage` is untouched;
    the TTL split is a pricing refinement WITHIN ``cache_creation_input_tokens``.
    """

    usage: TokenUsage
    session_id: str | None = None
    dedup_key: str | None = None
    source: str = ""
    agent_label: str | None = None
    is_sidechain: bool = False
    kept_but_suspect: bool = False
    # Descriptors the downstream layers read (duck-typed). Seam B fills the
    # transcript-derived ones; run/phase are tagged by Cycle-2 integration.
    model: str | None = None
    timestamp: str | None = None
    ts_epoch_ms: int | None = None
    run: str | None = None
    phase: str | None = None
    # Cache-write TTL split (pricing refinement within cache_creation_input_tokens,
    # NOT a 5th category). ``None`` when the transcript carried no nested split, so
    # pricing falls back to the flat 5m approximation.
    cache_creation_5m: int | None = None
    cache_creation_1h: int | None = None


class RunStatus(Enum):
    """Run-level classification for the Seam-A cost oracle (fail-loud).

    A failed run is NOT a $0 success. ``SUCCESS`` and ``FAILURE`` keep the exact
    string values the :mod:`scripts.tokenmeter_result` mirror used, so the classifier
    is unaffected by adopting the canonical enum. ``SUCCESS_ZERO_COST`` names the
    distinct "real tokens spent but $0 billed" case (e.g. fully cache-served or an
    unpriced/synthetic model) — tokens were genuinely produced, so it is a flavour of
    SUCCESS, never a FAILURE.
    """

    SUCCESS = "success"
    SUCCESS_ZERO_COST = "success_zero_cost"
    FAILURE = "failure"


@runtime_checkable
class TokenCounter(Protocol):
    """Structural token-counter contract used by :mod:`scripts.tokenmeter_static`.

    ``count`` returns a token count, or ``None`` when the counter cannot count (e.g.
    the optional Anthropic ``count_tokens`` HTTP path is unavailable) so the caller
    falls back to the deterministic char approximation. Implementations MAY expose a
    ``source`` string attribute (``measured`` / ``approximated``); the static layer
    reads it via ``getattr`` with a default, so it is NOT part of the required
    structural contract.
    """

    def count(self, text: str) -> int | None: ...
