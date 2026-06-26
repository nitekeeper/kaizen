"""Token-meter Seam A — result cost oracle (AI-2).

This module parses a Claude CLI *result envelope* (the JSON object a
``claude -p ... --output-format json`` run emits, mirrored by the
``FakeCliRunner`` shape in ``scripts.host_executor``) into a typed
:class:`ResultObject`, and classifies a run as :class:`RunStatus` SUCCESS or
FAILURE.

It is the "cost oracle": it reports what the CLI *said* it spent
(``total_cost_usd`` + ``usage``). The transcript ground truth (Seam B,
:mod:`scripts.tokenmeter_transcript`) is reconciled against it.

FAIL-LOUD classification — a failed run is NOT a $0 success. A run is FAILURE
when ANY of these holds:
  * ``is_error`` is truthy;
  * the raw result is 0 bytes (empty);
  * the raw result is unparseable JSON;
  * ``total_cost_usd == 0`` AND every token count is 0 (the CLI produced no
    measurable work — treating that as a free success would silently hide a
    broken run).

A run that produced real tokens at a ``$0`` bill (an unpriced/synthetic model or a
fully cache-served turn) is the distinct :attr:`RunStatus.SUCCESS_ZERO_COST` — a
flavour of success, never a FAILURE — so the "ran fine, billed nothing" state stays
visible rather than collapsing into plain SUCCESS. Any non-zero cost is SUCCESS.

The runner is INJECTABLE (:func:`run_and_classify`) and matches the
``FakeCliRunner`` shape (``async __call__(argv, cwd)`` returning the result
dict), so tests never spawn a real ``claude``.

SECURITY: result content is DATA. Parsed with ``json.loads`` only — no
``eval``/``exec``/shell. Stdlib-only.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# Shared token-meter types live in `scripts.tokenmeter_model` (owned by AI-1).
# Import when present; fall back to a spec-faithful local mirror so this module
# is self-consistent and testable before that sibling deliverable merges. Field
# names are the shared contract, so the real types win transparently at merge.
try:  # pragma: no cover - exercised by whichever path is present at import time
    from scripts.tokenmeter_model import RunStatus, TokenUsage
except ImportError:  # pragma: no cover
    from enum import Enum

    @dataclass(frozen=True)
    class TokenUsage:  # type: ignore[no-redef]
        input_tokens: int = 0
        output_tokens: int = 0
        cache_creation_input_tokens: int = 0
        cache_read_input_tokens: int = 0

    class RunStatus(Enum):  # type: ignore[no-redef]
        SUCCESS = "success"
        SUCCESS_ZERO_COST = "success_zero_cost"
        FAILURE = "failure"


_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)

# A runner matching the FakeCliRunner / real_cli_runner shape: awaitable call
# taking (argv, cwd) and resolving to the raw result (dict | str | bytes).
Runner = Callable[[Sequence[str], Any], Awaitable[Any]]


@dataclass(frozen=True)
class ResultObject:
    """Typed view of a CLI result envelope (the cost oracle's record)."""

    total_cost_usd: float
    usage: TokenUsage
    model_usage: dict[str, Any]
    session_id: str | None
    num_turns: int
    duration_ms: int
    is_error: bool
    stop_reason: str | None


def _harden_token(value: Any) -> int:
    """Coerce one raw usage value to a non-negative int (reject bool/non-int)."""
    if isinstance(value, bool):
        return 0
    if not isinstance(value, int):
        return 0
    return value if value > 0 else 0


def _coerce_float(value: Any) -> float:
    """Coerce a cost value to float; non-numeric (incl. bool) → 0.0."""
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, int | float):
        return float(value)
    return 0.0


def _coerce_int(value: Any) -> int:
    """Coerce a count value to int; non-numeric (incl. bool) → 0."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _usage_from(raw: Any) -> TokenUsage:
    """Build a hardened :class:`TokenUsage` from a raw ``usage`` mapping."""
    mapping = raw if isinstance(raw, Mapping) else {}
    return TokenUsage(**{name: _harden_token(mapping.get(name)) for name in _USAGE_FIELDS})


def parse_result(raw: Any) -> ResultObject:
    """Parse a raw CLI result into a :class:`ResultObject`.

    ``raw`` may be a decoded mapping or a JSON ``str``/``bytes`` blob. Raises
    :class:`ValueError` on a 0-byte (empty) blob or unparseable JSON — those are
    failure signals the classifier maps to :attr:`RunStatus.FAILURE` rather than
    fabricating a $0 success.
    """
    if isinstance(raw, bytes | bytearray):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        if not raw.strip():
            raise ValueError("empty (0-byte) result blob")
        raw = json.loads(raw)  # raises ValueError on bad JSON
    if not isinstance(raw, Mapping):
        raise ValueError(f"result is not a JSON object: {type(raw).__name__}")

    model_usage = raw.get("modelUsage")
    if not isinstance(model_usage, Mapping):
        model_usage = raw.get("model_usage")
    model_usage = dict(model_usage) if isinstance(model_usage, Mapping) else {}

    session_id = raw.get("session_id")
    stop_reason = raw.get("stop_reason")
    return ResultObject(
        total_cost_usd=_coerce_float(raw.get("total_cost_usd")),
        usage=_usage_from(raw.get("usage")),
        model_usage=model_usage,
        session_id=session_id if isinstance(session_id, str) else None,
        num_turns=_coerce_int(raw.get("num_turns")),
        duration_ms=_coerce_int(raw.get("duration_ms")),
        is_error=bool(raw.get("is_error")),
        stop_reason=stop_reason if isinstance(stop_reason, str) else None,
    )


def _is_zero_tokens(usage: TokenUsage) -> bool:
    return all(getattr(usage, name) == 0 for name in _USAGE_FIELDS)


def classify_result(raw: Any) -> RunStatus:
    """Classify a raw CLI result as :class:`RunStatus` (fail-loud).

    Three terminal states:

    * **FAILURE** — 0-byte/unparseable raw, ``is_error``, or ``total_cost_usd == 0``
      AND every token count is 0 (the CLI produced no measurable work; treating that
      as a free success would hide a broken run).
    * **SUCCESS_ZERO_COST** — real tokens were produced but the bill is ``$0`` (an
      unpriced/synthetic model, or a fully cache-served turn). The run ran fine; the
      distinct member keeps that visible instead of collapsing it into ``SUCCESS``.
    * **SUCCESS** — a non-zero cost.

    A terminal ``blocked`` task outcome that still spent tokens is a SUCCESS *run*
    (the task outcome is a separate concern from whether the run executed); at $0 it
    surfaces as SUCCESS_ZERO_COST.
    """
    try:
        result = parse_result(raw)
    except (ValueError, TypeError):
        return RunStatus.FAILURE
    if result.is_error:
        return RunStatus.FAILURE
    if result.total_cost_usd == 0:
        if _is_zero_tokens(result.usage):
            return RunStatus.FAILURE
        return RunStatus.SUCCESS_ZERO_COST
    return RunStatus.SUCCESS


async def run_and_classify(
    runner: Runner,
    argv: Sequence[str],
    cwd: Any,
) -> tuple[RunStatus, ResultObject | None]:
    """Invoke an INJECTABLE runner and classify its result (no real ``claude``).

    ``runner`` matches the ``FakeCliRunner``/``real_cli_runner`` shape: an
    awaitable called with ``(argv, cwd)`` resolving to the raw result. Returns
    ``(status, result_object_or_None)`` — the :class:`ResultObject` is ``None``
    only when the raw result is 0-byte/unparseable (a FAILURE).
    """
    raw = await runner(argv, cwd)
    status = classify_result(raw)
    try:
        result = parse_result(raw)
    except (ValueError, TypeError):
        result = None
    return status, result
