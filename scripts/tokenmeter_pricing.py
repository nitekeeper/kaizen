"""Cache-aware, four-category token cost model for kaizen's tokenmeter.

This module turns a :class:`~scripts.tokenmeter_model.TokenUsage` (the canonical
per-message usage record) into a USD cost broken down across the four billable
categories Anthropic charges for:

* **input**       — fresh (uncached) input tokens, billed at the model base rate;
* **output**      — generated tokens, billed at the model output rate;
* **cache read**  — tokens served from the prompt cache (a steep discount);
* **cache write** — tokens written into the prompt cache, *TTL-aware*: a 5-minute
  ephemeral write costs more than base input, a 1-hour ephemeral write costs more
  still.

Design constraints (kaizen rule F2 — stdlib-only, ruff + bandit clean):

* **No third-party dependencies.** Base prices live in a plain ``dict``
  (:data:`PRICING`); category rates are *derived* from the base input price via
  fixed multipliers so there is a single source of truth per model.
* **Cache-write TTL split is read from the transcript, never guessed.** When the
  usage record carries the per-TTL breakdown
  (``cache_creation.ephemeral_5m_input_tokens`` /
  ``ephemeral_1h_input_tokens``) the two buckets are priced separately and the
  result is marked ``source="exact"``. When only the flat
  ``cache_creation_input_tokens`` total is present we conservatively treat the
  whole amount as a 5-minute write and mark ``source="approximated"``.
* **Unknown / synthetic models never crash and never lie.** They return
  ``priced=False`` with every cost ``0.0`` while *keeping* the raw token counts,
  so token accounting stays correct even when dollar accounting cannot.

Pricing is grounded against the ``claude-api`` skill reference (not memory) as of
:data:`PRICING_AS_OF`. Installers can override or extend prices at runtime via the
``KAIZEN_PRICING_JSON`` environment variable (a JSON object merged over
:data:`PRICING`).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a hard import cycle.
    from scripts.tokenmeter_model import TokenUsage

# Date the base prices below were last reconciled against the claude-api skill.
PRICING_AS_OF = "2026-06-25"

# ── Category multipliers (x the model base *input* $/Mtok) ──────────────────
# Cache reads are heavily discounted; cache writes carry a TTL premium.
CACHE_READ = 0.10
CACHE_WRITE_5M = 1.25
CACHE_WRITE_1H = 2.00

# Sentinel substring used by the harness for non-billable synthetic usage rows.
SYNTHETIC_MARKER = "<synthetic>"

# ── Base prices, $ per million tokens, per canonical model ──────────────────
# Grounded against the claude-api skill as of PRICING_AS_OF. The current 4.x
# generation is flat-rate (no >200k-context premium); the optional
# "context_tiers" key is reserved for when that changes — today every tier
# multiplier is 1.0, so it is simply omitted.
PRICING: dict[str, dict[str, Any]] = {
    "claude-opus-4-8": {"input": 5.0, "output": 25.0},
    "claude-opus-4-7": {"input": 5.0, "output": 25.0},
    "claude-opus-4-6": {"input": 5.0, "output": 25.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
    "fable-5": {"input": 10.0, "output": 50.0},
}

# Explicit canonicalization for known dated snapshots / shorthands. Applied
# BEFORE the generic date-suffix strip and BEFORE any PRICING lookup.
ALIASES: dict[str, str] = {
    "claude-opus-4-8-20260514": "claude-opus-4-8",
    "claude-opus-4-7-20251101": "claude-opus-4-7",
    "claude-opus-4-6-20250901": "claude-opus-4-6",
    "claude-sonnet-4-6-20250901": "claude-sonnet-4-6",
    "claude-haiku-4-5-20250901": "claude-haiku-4-5",
    "fable-5-20260101": "fable-5",
    # Convenience shorthands.
    "opus": "claude-opus-4-8",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5",
}

# Trailing date snapshot suffix, e.g. "-20260514".
_DATE_SUFFIX = re.compile(r"-\d{6,8}$")


def canonicalize(model: str | None) -> str:
    """Map a (possibly dated / aliased) model id to its canonical key.

    Resolution order: explicit :data:`ALIASES` first, then strip a trailing
    ``-YYYYMMDD`` snapshot suffix and re-check the alias table. The returned
    string is not guaranteed to be in :data:`PRICING` — callers treat a miss as
    an unknown (unpriced) model.
    """
    if not model:
        return ""
    key = model.strip()
    if key in ALIASES:
        return ALIASES[key]
    stripped = _DATE_SUFFIX.sub("", key)
    if stripped in ALIASES:
        return ALIASES[stripped]
    return stripped


def _load_pricing() -> dict[str, dict[str, Any]]:
    """Effective price table: the ``KAIZEN_PRICING_JSON`` override merged over
    the built-in :data:`PRICING`. Malformed JSON is ignored (the built-in table
    is always a safe fallback)."""
    table: dict[str, dict[str, Any]] = {k: dict(v) for k, v in PRICING.items()}
    raw = os.environ.get("KAIZEN_PRICING_JSON")
    if not raw:
        return table
    try:
        override = json.loads(raw)
    except (ValueError, TypeError):
        return table
    if not isinstance(override, dict):
        return table
    for model, entry in override.items():
        if isinstance(entry, dict):
            table[str(model)] = {**table.get(str(model), {}), **entry}
    return table


def _field(usage: Any, name: str) -> Any:
    """Read ``name`` from a usage record that may be an object or a dict."""
    if isinstance(usage, dict):
        return usage.get(name)
    return getattr(usage, name, None)


def _as_int(value: Any) -> int:
    """Coerce a token count to a non-negative int; treat junk / None as 0."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _context_multiplier(entry: dict[str, Any], input_tokens: int) -> float:
    """Long-context premium for the input rate. Returns 1.0 unless the model
    entry declares ``context_tiers`` (a list of ``[threshold, multiplier]``);
    the current 4.x flat-rate models declare none, so this is always 1.0 today.
    """
    tiers = entry.get("context_tiers")
    if not tiers:
        return 1.0
    multiplier = 1.0
    for threshold, value in sorted(tiers, key=lambda t: t[0]):
        if input_tokens >= threshold:
            multiplier = float(value)
    return multiplier


@dataclass(frozen=True)
class CostBreakdown:
    """Four-category cost result. ``total_cost`` is the sum of the four cost
    fields. ``source`` is ``"exact"`` (TTL split read from the transcript or no
    cache writes), ``"approximated"`` (flat cache-write total split as 5m), or
    ``"unpriced"`` (unknown / synthetic model). Token counts are always retained
    even when ``priced`` is ``False``."""

    model: str
    canonical_model: str
    priced: bool
    source: str
    # Token counts (always populated).
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_5m_tokens: int
    cache_write_1h_tokens: int
    # USD costs (0.0 when unpriced).
    input_cost: float
    output_cost: float
    cache_read_cost: float
    cache_write_cost: float
    total_cost: float

    def to_dict(self) -> dict[str, Any]:
        """Plain-dict view (stdlib only) for JSON serialization / aggregation."""
        return asdict(self)


def cost_usd(
    usage: TokenUsage | Any,
    model: str | None,
    *,
    cache_creation: Any = None,
) -> CostBreakdown:
    """Price one usage record for ``model`` into a :class:`CostBreakdown`.

    Unknown or ``<synthetic>`` models yield ``priced=False`` with all costs
    ``0.0`` while keeping the token counts. The cache-write TTL split is taken
    from the transcript when present (``source="exact"``) and approximated as a
    5-minute write otherwise (``source="approximated"``).

    The four-category :class:`~scripts.tokenmeter_model.TokenUsage` deliberately
    does NOT carry the cache-write TTL split, so a caller pricing a real record
    passes it explicitly via ``cache_creation`` (a mapping/object exposing
    ``ephemeral_5m_input_tokens`` / ``ephemeral_1h_input_tokens``). When that
    argument is ``None`` we fall back to reading a ``cache_creation`` attribute off
    ``usage`` itself (the duck-typed shape the unit tests use). Either way the split
    is a pricing refinement WITHIN ``cache_creation_input_tokens`` — never a fifth
    token category.
    """
    input_tokens = _as_int(_field(usage, "input_tokens"))
    output_tokens = _as_int(_field(usage, "output_tokens"))
    cache_read_tokens = _as_int(_field(usage, "cache_read_input_tokens"))

    # Resolve the cache-write TTL split. Prefer the split the caller passed
    # explicitly (real records carry it on the UsageRecord, not on TokenUsage),
    # then a ``cache_creation`` attribute on the usage object, then the flat total.
    cc = cache_creation if cache_creation is not None else _field(usage, "cache_creation")
    e5 = e1 = None
    if cc is not None:
        e5 = _field(cc, "ephemeral_5m_input_tokens")
        e1 = _field(cc, "ephemeral_1h_input_tokens")
    if e5 is None:
        e5 = _field(usage, "ephemeral_5m_input_tokens")
    if e1 is None:
        e1 = _field(usage, "ephemeral_1h_input_tokens")

    flat_cache_write = _as_int(_field(usage, "cache_creation_input_tokens"))

    if e5 is not None or e1 is not None:
        write_5m = _as_int(e5)
        write_1h = _as_int(e1)
        split_source = "exact"
    else:
        write_5m = flat_cache_write
        write_1h = 0
        split_source = "approximated" if flat_cache_write > 0 else "exact"

    canonical = canonicalize(model)
    is_synthetic = model is None or SYNTHETIC_MARKER in str(model)
    entry = None if is_synthetic else _load_pricing().get(canonical)

    if entry is None:
        # Unknown / synthetic: keep tokens, zero out every cost.
        return CostBreakdown(
            model=str(model) if model is not None else "",
            canonical_model=canonical,
            priced=False,
            source="unpriced",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_5m_tokens=write_5m,
            cache_write_1h_tokens=write_1h,
            input_cost=0.0,
            output_cost=0.0,
            cache_read_cost=0.0,
            cache_write_cost=0.0,
            total_cost=0.0,
        )

    base_in = float(entry["input"])
    base_out = float(entry["output"])
    ctx = _context_multiplier(entry, input_tokens)
    per_mtok = 1_000_000.0

    input_cost = input_tokens / per_mtok * base_in * ctx
    output_cost = output_tokens / per_mtok * base_out
    cache_read_cost = cache_read_tokens / per_mtok * base_in * ctx * CACHE_READ
    cache_write_cost = (
        (write_5m * CACHE_WRITE_5M + write_1h * CACHE_WRITE_1H) / per_mtok * base_in * ctx
    )
    total_cost = input_cost + output_cost + cache_read_cost + cache_write_cost

    return CostBreakdown(
        model=str(model),
        canonical_model=canonical,
        priced=True,
        source=split_source,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_5m_tokens=write_5m,
        cache_write_1h_tokens=write_1h,
        input_cost=input_cost,
        output_cost=output_cost,
        cache_read_cost=cache_read_cost,
        cache_write_cost=cache_write_cost,
        total_cost=total_cost,
    )
