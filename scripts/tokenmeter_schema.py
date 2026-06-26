"""Token-meter report assembly + cost reconciliation (AI-5).

This module is the *assembly + reconciliation* layer of kaizen's token meter. It
takes the two seams produced upstream —

* **Seam B** (ground truth): the de-duplicated :class:`UsageRecord` list walked
  out of Claude Code's on-disk transcripts by
  :mod:`scripts.tokenmeter_transcript`. Sidechain (sub-agent) tokens are INCLUDED;
  this is ALWAYS the authoritative headline.
* **Seam A** (cost oracle): the CLI result envelope parsed by
  :mod:`scripts.tokenmeter_result`. It reports what the CLI *said* it spent and is
  used for *validation only*, never as the headline.

— plus the static footprint rows from :mod:`scripts.tokenmeter_static`, the
per-record pricing from :mod:`scripts.tokenmeter_pricing`, and the run outcome,
and assembles a single ``BenchmarkReport`` (a pure ``dict``).

Hard rules baked in here (each enforced by :func:`validate_report` or a guard):

* **No null figures.** Every row carries a non-null ``source`` (one of
  ``measured`` / ``approximated`` / ``oracle``) and ``mode`` (``static`` /
  ``dynamic``); the validator raises on a null/missing one.
* **The four token categories are NEVER summed into one token total.** Category
  rows stay split (input / output / cache-write / cache-read). Cost (USD) is the
  ONLY legitimate cross-category scalar — phase/role rows aggregate by cost.
* **tokscale-compatible emitted field names** — the per-record evidence and
  rollups emit ``input_tokens`` / ``output_tokens`` /
  ``cache_creation_input_tokens`` / ``cache_read_input_tokens`` / ``model`` /
  ``timestamp`` / ``session_id`` verbatim.
* **Control-vector equality gate.** ``before/after`` deltas are REFUSED (a
  :class:`ControlDriftError` is raised) if any control drifted: ``model``,
  ``effort``, ``scenario_hash``, ``cycles``, ``transport``,
  ``rate_table_as_of`` must match between the two reports.
* **Dynamic aggregate shape.** Every dynamic figure carries ``{n, mean, cv,
  confidence}`` rather than a baked single-run scalar. Cycle-1 (a single run)
  fills ``n=1``, ``cv=null``, ``confidence='directional'``.
* **Cost-oracle reconciliation.** The Seam-B computed total is reconciled against
  the Seam-A oracle total with tolerance ``max(1% relative, $0.005 floor)``:
  within tolerance AGREE; 1-5% SOFT (emitted, flagged approximated); >5% HARD
  (blocks the ``validated`` status). Both totals are ALWAYS recorded together
  with a ``subagent_gap`` discriminator that attributes the gap to a
  ``subagent-boundary`` (Seam A approximates the orchestrator-only share) or to
  ``pricing``.

SECURITY: all upstream content is DATA. This module computes over already-parsed
records; it never evals, execs, or shells out. Stdlib-only.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime

from scripts.tokenmeter_pricing import PRICING_AS_OF, cost_usd

# ── Constants ────────────────────────────────────────────────────────────────

SCHEMA_VERSION = "1.0"
BENCHMARK_VERSION = "tokenmeter-1.0"

#: The four billable token categories — kept SPLIT, never summed into one total.
CATEGORY_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)

#: Controls that must be equal for a before/after delta to be legitimate.
CONTROL_KEYS = (
    "model",
    "effort",
    "scenario_hash",
    "cycles",
    "transport",
    "rate_table_as_of",
)

#: Metadata keys that must be present (and non-null) on every assembled report.
METADATA_KEYS = (
    "target",
    "target_commit",
    "transport",
    "model",
    "effort",
    "caching",
    "cycles",
    "subject",
    "scenario_source",
    "scenario_hash",
    "n_runs",
    "timestamp",
    "benchmark_version",
    "claude_config_dir",
    "local_tz",
    "rate_table_as_of",
)

NA = "n/a"

SOURCE_MEASURED = "measured"
SOURCE_APPROX = "approximated"
SOURCE_ORACLE = "oracle"
VALID_SOURCES = (SOURCE_MEASURED, SOURCE_APPROX, SOURCE_ORACLE)

MODE_STATIC = "static"
MODE_DYNAMIC = "dynamic"
VALID_MODES = (MODE_STATIC, MODE_DYNAMIC)

VALID_KINDS = ("category", "phase", "role", "overhead")

#: The :class:`~scripts.tokenmeter_result.RunStatus` FAILURE value, duplicated here
#: as a literal so this assembly layer stays import-light (it never imports the
#: Seam-A result/model layer). Kept in lockstep with ``RunStatus.FAILURE.value``.
RUN_FAILURE = "failure"

#: Reconciliation tolerance: relative 1% OR a $0.005 absolute floor (whichever
#: is larger) is considered AGREEMENT.
RECONCILE_REL_TOL = 0.01
RECONCILE_ABS_FLOOR = 0.005
RECONCILE_SOFT_PCT = 5.0

#: Confidence threshold on the coefficient of variation for a multi-run figure.
_STABLE_CV = 0.15

#: Saturating ceiling for aggregate sums (kept far above any real workload).
SATURATION_MAX = 2**63 - 1


class ReportValidationError(ValueError):
    """Raised when an assembled report violates a hard invariant."""


class ControlDriftError(ValueError):
    """Raised when a before/after delta is requested across drifted controls."""


# ── Record accessors (duck-typed: object OR mapping) ─────────────────────────


def _get(obj, name, default=None):
    """Read ``name`` from an object or a mapping, with a default."""
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _usage_field(rec, name):
    """Non-negative int token count for ``name`` from a record's usage."""
    usage = _get(rec, "usage")
    raw = _get(usage if usage is not None else rec, name, 0)
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


def _rec_model(rec, default):
    return _get(rec, "model") or default


def _is_suspect(rec):
    return bool(_get(rec, "kept_but_suspect", False))


def _is_sidechain(rec):
    return bool(_get(rec, "is_sidechain", False))


def _record_cost(rec, default_model):
    """Price one record with the pricing model (record model → default).

    The cache-write TTL split lives on the record (``cache_creation_5m`` /
    ``cache_creation_1h``), NOT on the four-category ``usage``, so it is handed to
    :func:`~scripts.tokenmeter_pricing.cost_usd` explicitly — that is what fires the
    EXACT TTL-split pricing path (1h at 2.0x) on a real parsed record. When neither
    is set we pass ``None`` and pricing falls back to the flat-5m approximation.
    """
    usage = _get(rec, "usage", rec)
    e5 = _get(rec, "cache_creation_5m")
    e1 = _get(rec, "cache_creation_1h")
    cache_creation = None
    if e5 is not None or e1 is not None:
        cache_creation = {
            "ephemeral_5m_input_tokens": e5 or 0,
            "ephemeral_1h_input_tokens": e1 or 0,
        }
    return cost_usd(usage, _rec_model(rec, default_model), cache_creation=cache_creation)


# ── Dynamic aggregate shape ──────────────────────────────────────────────────


def dynamic_figure(values):
    """Build a ``{n, mean, cv, confidence}`` aggregate from per-run values.

    A single run yields ``cv=None`` and ``confidence='directional'`` (Cycle-1 is
    directional, never baked as a precise scalar). Multiple runs compute the
    coefficient of variation and tag ``stable`` (cv < 0.15) or ``noisy``.
    """
    vals = [float(v) for v in values]
    n = len(vals)
    if n == 0:
        return {"n": 0, "mean": 0.0, "cv": None, "confidence": "none"}
    mean = sum(vals) / n
    if n == 1:
        return {"n": 1, "mean": mean, "cv": None, "confidence": "directional"}
    variance = sum((v - mean) ** 2 for v in vals) / n
    std = variance**0.5
    cv = (std / mean) if mean else None
    confidence = "stable" if (cv is not None and cv < _STABLE_CV) else "noisy"
    return {"n": n, "mean": mean, "cv": cv, "confidence": confidence}


def _figure_value(fig):
    """Scalar comparison value of a figure (mean for dynamic, else the scalar)."""
    if fig is None:
        return None
    if isinstance(fig, Mapping):
        return fig.get("mean")
    if isinstance(fig, int | float):
        return fig
    return None


def _delta(before, after):
    if before is None or after is None:
        return None, None
    delta_abs = after - before
    delta_pct = (delta_abs / before * 100.0) if before else None
    return delta_abs, delta_pct


# ── Control-vector equality gate ─────────────────────────────────────────────


def control_vector(metadata):
    """Extract the control vector (the keys that must match for a delta)."""
    return {key: _get(metadata, key) for key in CONTROL_KEYS}


def controls_match(before_md, after_md):
    """True iff the two metadata blocks share an identical control vector."""
    return control_vector(before_md) == control_vector(after_md)


def assert_controls_match(before_md, after_md):
    """Raise :class:`ControlDriftError` if any control drifted between reports."""
    before = control_vector(before_md)
    after = control_vector(after_md)
    drifted = [key for key in CONTROL_KEYS if before.get(key) != after.get(key)]
    if drifted:
        detail = ", ".join(f"{key}: {before.get(key)!r} -> {after.get(key)!r}" for key in drifted)
        raise ControlDriftError(f"control vector drifted; before/after deltas refused ({detail})")


# ── Cost-oracle reconciliation ───────────────────────────────────────────────


def reconcile_cost(records, oracle, default_model):
    """Reconcile the Seam-B computed cost against the Seam-A oracle total.

    ``computed`` sums ``tokens * rate(model, category, ttl)`` over every record
    (sidechains INCLUDED — Seam B is the headline). ``seam_a`` is the oracle's
    ``total_cost_usd`` (validation only). Returns the ``cost_oracle`` block with
    BOTH totals always recorded, the ``reconciled`` verdict
    (``agree`` / ``soft`` / ``hard`` / ``unreconciled``), the divergence percent,
    and a ``divergence_cause`` that discriminates a subagent-boundary gap (the
    oracle approximates only the orchestrator-only share) from a pricing gap.
    """
    full = 0.0
    orchestrator_only = 0.0
    for rec in records:
        breakdown = _record_cost(rec, default_model)
        full += breakdown.total_cost
        if not _is_sidechain(rec):
            orchestrator_only += breakdown.total_cost

    computed = round(full, 10)
    seam_a = None if oracle is None else float(_get(oracle, "total_cost_usd", 0.0) or 0.0)

    if seam_a is None:
        return {
            "seam_a_total_cost_usd": None,
            "computed_total_cost_usd": computed,
            "reconciled": "unreconciled",
            "divergence_pct": None,
            "divergence_cause": None,
            "flagged_approximated": False,
            "blocks_validated": False,
        }

    diff = abs(computed - seam_a)
    tol = max(RECONCILE_REL_TOL * abs(seam_a), RECONCILE_ABS_FLOOR)
    divergence_pct = (diff / abs(seam_a) * 100.0) if seam_a else None

    if diff <= tol:
        reconciled = "agree"
    elif divergence_pct is not None and divergence_pct <= RECONCILE_SOFT_PCT:
        reconciled = "soft"
    else:
        reconciled = "hard"

    # subagent_gap discriminator: if the oracle is closer to the orchestrator-only
    # computed share than to the full (sidechain-included) computed total, the gap
    # is a Seam-A subagent-boundary artefact, not a pricing error.
    gap_full = abs(seam_a - full)
    gap_orchestrator = abs(seam_a - orchestrator_only)
    cause = "subagent-boundary" if gap_orchestrator < gap_full else "pricing"

    return {
        "seam_a_total_cost_usd": round(seam_a, 10),
        "computed_total_cost_usd": computed,
        "reconciled": reconciled,
        "divergence_pct": divergence_pct,
        "divergence_cause": cause,
        "flagged_approximated": reconciled == "soft",
        "blocks_validated": reconciled == "hard",
    }


# ── Row builders ─────────────────────────────────────────────────────────────


def _row(kind, name, mode, source, suspect, before_fig, after_fig):
    delta_abs, delta_pct = _delta(_figure_value(before_fig), _figure_value(after_fig))
    return {
        "row": name,
        "kind": kind,
        "mode": mode,
        "source": source,
        "suspect": bool(suspect),
        "before": before_fig,
        "after": after_fig,
        "delta_abs": delta_abs,
        "delta_pct": delta_pct,
    }


def _category_figure(records, field):
    per_run: dict = {}
    for rec in records:
        key = _get(rec, "run", "__single__")
        per_run[key] = per_run.get(key, 0) + _usage_field(rec, field)
    return dynamic_figure(list(per_run.values()))


def _category_rows(records, before_rows):
    # Category rows are DYNAMIC (measured) figures. A static-only report carries no
    # dynamic records, so emitting all-zero category rows tagged dynamic/measured
    # would be a lie — suppress them entirely when there is nothing measured.
    if not records:
        return []
    suspect = any(_is_suspect(rec) for rec in records)
    rows = []
    for field in CATEGORY_FIELDS:
        after = _category_figure(records, field)
        before = before_rows.get(("category", field))
        rows.append(_row("category", field, MODE_DYNAMIC, SOURCE_MEASURED, suspect, before, after))
    return rows


def _grouped_cost_rows(records, attr, kind, before_rows, default_model):
    groups: dict = {}
    for rec in records:
        key = _get(rec, attr)
        if key is None:
            continue
        groups.setdefault(key, []).append(rec)

    rows = []
    for key in sorted(groups, key=str):
        group = groups[key]
        per_run: dict = {}
        any_unpriced = False
        for rec in group:
            breakdown = _record_cost(rec, default_model)
            run_key = _get(rec, "run", "__single__")
            per_run[run_key] = per_run.get(run_key, 0.0) + breakdown.total_cost
            if not breakdown.priced:
                any_unpriced = True
        after = dynamic_figure(list(per_run.values()))
        before = before_rows.get((kind, key))
        source = SOURCE_APPROX if any_unpriced else SOURCE_MEASURED
        suspect = any(_is_suspect(rec) for rec in group)
        rows.append(_row(kind, key, MODE_DYNAMIC, source, suspect, before, after))
    return rows


def _static_files(static_rows):
    if isinstance(static_rows, Mapping):
        return static_rows.get("files", [])
    return list(static_rows or [])


def _overhead_rows(static_rows, before_rows):
    rows = []
    for entry in _static_files(static_rows):
        path = _get(entry, "path", "")
        token = _get(entry, "token_count")
        if token is None:
            token = _get(entry, "input", 0)
        source = _get(entry, "source") or SOURCE_APPROX
        before = before_rows.get(("overhead", path))
        rows.append(_row("overhead", path, MODE_STATIC, source, False, before, token))
    return rows


def _derived_rows(records, outcomes, cost_oracle, before_derived):
    total_input = sum(_usage_field(rec, "input_tokens") for rec in records)
    total_read = sum(_usage_field(rec, "cache_read_input_tokens") for rec in records)
    denom = total_read + total_input
    cache_hit_rate = (total_read / denom) if denom else None

    n_calls = len(records)
    # tokens-per-call is emitted PER CATEGORY — never as one summed scalar. A single
    # summed "tokens_per_call" is dominated by cache_read (~99% of token COUNT) and
    # is really "cache-reads per call"; tokens and cost rank the categories
    # OPPOSITELY (cache_read dominates count, output dominates cost), so collapsing
    # them hides the signal. We deliberately emit NO input+output "total".
    specs = [("cache_hit_rate", cache_hit_rate, SOURCE_MEASURED, MODE_DYNAMIC)]
    for field in CATEGORY_FIELDS:
        total_field = sum(_usage_field(rec, field) for rec in records)
        per_call = (total_field / n_calls) if n_calls else None
        specs.append((f"tokens_per_call.{field}", per_call, SOURCE_MEASURED, MODE_DYNAMIC))

    headline_cost = cost_oracle["computed_total_cost_usd"]
    succeeded = int(_get(outcomes, "cycles_succeeded", 0) or 0)
    effective_unit_cost = (headline_cost / succeeded) if succeeded else None

    # cost figures carry SOURCE_ORACLE (validated against the Seam-A oracle) but are
    # MODE_DYNAMIC — they are derived from the dynamic records, not a static estimate.
    specs.append(("cost_usd", headline_cost, SOURCE_ORACLE, MODE_DYNAMIC))
    specs.append(
        ("effective_unit_cost_per_accepted_cycle", effective_unit_cost, SOURCE_ORACLE, MODE_DYNAMIC)
    )

    return [
        {
            "row": name,
            "before": before_derived.get(name),
            "after": value,
            "source": source,
            "mode": mode,
        }
        for name, value, source, mode in specs
    ]


def _index_rows(rows):
    return {(_get(r, "kind"), _get(r, "row")): _get(r, "after") for r in rows}


def _index_derived(derived):
    return {_get(d, "row"): _get(d, "after") for d in derived}


def _count_runs(records):
    runs = {_get(rec, "run") for rec in records}
    runs.discard(None)
    return len(runs) or (1 if records else 0)


def _scenario_hash(scenario_source):
    return hashlib.sha256((scenario_source or "").encode("utf-8")).hexdigest()[:16]


def _runs_block(run_statuses):
    """Summarize the per-run Seam-A classifications into the report's ``runs`` block.

    ALWAYS present (an empty summary when no statuses are threaded) so a report can
    never look CLEAN while a run silently FAILED (design §4, fail-loud): an
    all-FAILURE harvest yields empty category rows + ``reconciled='unreconciled'``,
    which on its own reads exactly like a no-op clean run — this ``runs`` block is
    the explicit failure marker that prevents that masking. Each status normalizes
    to its ``RunStatus.value`` string ("success" / "success_zero_cost" / "failure"),
    so the emitted JSON stays clean (never ``"RunStatus.FAILURE"``).
    """
    statuses = [getattr(st, "value", None) or str(st) for st in (run_statuses or [])]
    failed = sum(1 for s in statuses if s == RUN_FAILURE)
    return {
        "n_runs": len(statuses),
        "statuses": statuses,
        "runs_failed": failed,
        "any_failed": failed > 0,
        "all_failed": bool(statuses) and failed == len(statuses),
    }


# ── Assembly ─────────────────────────────────────────────────────────────────


def assemble(
    records, static_rows, *, outcomes, oracle=None, before=None, metadata=None, run_statuses=None
):
    """Assemble a ``BenchmarkReport`` (pure dict) from the upstream seams.

    ``records`` are Seam-B :class:`UsageRecord`-shaped objects (or dicts);
    ``static_rows`` is the static footprint (the ``files`` list or the full
    :func:`scripts.tokenmeter_static.static_footprint` dict); ``outcomes`` carries
    ``cycles_succeeded`` / ``cycles_abandoned`` / ``pr_opened`` / ``tests_green``;
    ``oracle`` is the Seam-A :class:`ResultObject` (or any object exposing
    ``total_cost_usd``) used for reconciliation only; ``before`` is a prior report
    to delta against — its presence triggers the control-vector equality gate;
    ``metadata`` supplies the run descriptors; ``run_statuses`` is the per-run
    :class:`~scripts.tokenmeter_result.RunStatus` list from the dynamic harness
    (folded into the top-level ``runs`` block so an all-FAILURE harvest can never
    emit a report that reads clean — design §4 fail-loud).

    The returned report is validated by :func:`validate_report` before return, so
    a structurally-invalid report raises rather than escaping silently.
    """
    records = list(records)
    meta_in = dict(metadata or {})
    default_model = meta_in.get("model") or ""
    now = datetime.now().astimezone()

    scenario_source = meta_in.get("scenario_source", "")
    md = {
        "target": meta_in.get("target", ""),
        "target_commit": meta_in.get("target_commit", ""),
        "transport": meta_in.get("transport", ""),
        "model": default_model,
        "effort": meta_in.get("effort", ""),
        "caching": meta_in.get("caching", ""),
        "cycles": meta_in.get("cycles", 0),
        "subject": meta_in.get("subject", ""),
        "scenario_source": scenario_source,
        "scenario_hash": meta_in.get("scenario_hash") or _scenario_hash(scenario_source),
        "n_runs": meta_in.get("n_runs", _count_runs(records)),
        "timestamp": meta_in.get("timestamp") or now.isoformat(),
        "benchmark_version": meta_in.get("benchmark_version", BENCHMARK_VERSION),
        "claude_config_dir": meta_in.get("claude_config_dir", ""),
        "local_tz": meta_in.get("local_tz") or (now.tzname() or str(now.tzinfo)),
        "rate_table_as_of": meta_in.get("rate_table_as_of", PRICING_AS_OF),
    }

    # CONTROL-VECTOR EQUALITY GATE — a delta across drifted controls is refused.
    if before is not None:
        assert_controls_match(_get(before, "metadata", {}), md)

    before_rows = _index_rows(_get(before, "rows", []) if before else [])
    before_derived = _index_derived(_get(before, "derived", []) if before else [])

    rows = []
    rows += _category_rows(records, before_rows)
    rows += _grouped_cost_rows(records, "phase", "phase", before_rows, default_model)
    rows += _grouped_cost_rows(records, "agent_label", "role", before_rows, default_model)
    rows += _overhead_rows(static_rows, before_rows)

    cost_oracle = reconcile_cost(records, oracle, default_model)
    derived = _derived_rows(records, outcomes, cost_oracle, before_derived)

    outcome = {
        "cycles_succeeded": int(_get(outcomes, "cycles_succeeded", 0) or 0),
        "cycles_abandoned": int(_get(outcomes, "cycles_abandoned", 0) or 0),
        "pr_opened": bool(_get(outcomes, "pr_opened", False)),
        "tests_green": bool(_get(outcomes, "tests_green", False)),
    }

    report = {
        "schema_version": SCHEMA_VERSION,
        "metadata": md,
        "rows": rows,
        "derived": derived,
        "outcome": outcome,
        "cost_oracle": cost_oracle,
        "runs": _runs_block(run_statuses),
    }
    validate_report(report)
    return report


# ── Validation ───────────────────────────────────────────────────────────────


def validate_report(report):
    """Validate a report's hard invariants; raise :class:`ReportValidationError`.

    Checks: metadata keys present and non-null; every row has a non-null, valid
    ``source`` + ``mode`` + ``kind``; every derived figure has a non-null, valid
    ``source`` + ``mode``; the cost-oracle block records both totals + the verdict
    keys; the ``runs`` block records the per-run status summary (the fail-loud
    marker).
    """
    if not isinstance(report, Mapping):
        raise ReportValidationError("report must be a mapping")

    md = report.get("metadata")
    if not isinstance(md, Mapping):
        raise ReportValidationError("metadata missing")
    for key in METADATA_KEYS:
        if key not in md or md[key] is None:
            raise ReportValidationError(f"metadata.{key} is null/missing")

    rows = report.get("rows")
    if not isinstance(rows, list):
        raise ReportValidationError("rows missing")
    for idx, row in enumerate(rows):
        source = row.get("source")
        if source is None or source not in VALID_SOURCES:
            raise ReportValidationError(f"rows[{idx}].source null/invalid: {source!r}")
        mode = row.get("mode")
        if mode is None or mode not in VALID_MODES:
            raise ReportValidationError(f"rows[{idx}].mode null/invalid: {mode!r}")
        if row.get("kind") not in VALID_KINDS:
            raise ReportValidationError(f"rows[{idx}].kind invalid: {row.get('kind')!r}")

    derived = report.get("derived")
    if not isinstance(derived, list):
        raise ReportValidationError("derived missing")
    for idx, entry in enumerate(derived):
        source = entry.get("source")
        if source is None or source not in VALID_SOURCES:
            raise ReportValidationError(f"derived[{idx}].source null/invalid: {source!r}")
        # Derived figures carry a mode too (enforced consistently with rows); they
        # are dynamic-side diagnostics, never a static estimate.
        mode = entry.get("mode")
        if mode is None or mode not in VALID_MODES:
            raise ReportValidationError(f"derived[{idx}].mode null/invalid: {mode!r}")

    cost_oracle = report.get("cost_oracle")
    if not isinstance(cost_oracle, Mapping):
        raise ReportValidationError("cost_oracle missing")
    for key in (
        "seam_a_total_cost_usd",
        "computed_total_cost_usd",
        "reconciled",
        "divergence_pct",
        "divergence_cause",
    ):
        if key not in cost_oracle:
            raise ReportValidationError(f"cost_oracle.{key} missing")

    # The per-run status block — the fail-loud marker that stops an all-FAILURE
    # harvest from emitting a report that reads clean (design §4).
    runs = report.get("runs")
    if not isinstance(runs, Mapping):
        raise ReportValidationError("runs block missing")
    for key in ("n_runs", "statuses", "runs_failed", "any_failed", "all_failed"):
        if key not in runs:
            raise ReportValidationError(f"runs.{key} missing")
    return True


def is_validated(report):
    """True unless the cost oracle reported a HARD (>5%) divergence."""
    return _get(report.get("cost_oracle", {}), "reconciled") != "hard"
