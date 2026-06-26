"""Token-meter renderers + aggregation (AI-5).

Renderers turn an assembled ``BenchmarkReport`` (see
:mod:`scripts.tokenmeter_schema`) into the output formats kaizen consumes, and a
small aggregation layer rolls raw Seam-B records up for downstream tools.

* :func:`render_json` — canonical, deterministic JSON (sorted keys).
* :func:`render_markdown` — a human table with ``BEFORE | AFTER | delta_abs |
  delta_pct`` plus ``source`` + ``mode`` columns and an outcome footer. Static
  cells that do not apply render the :data:`~scripts.tokenmeter_schema.NA` marker,
  never ``0``.
* :func:`render_csv` — one CSV row per report row.
* :func:`render_jsonl` — per-record evidence, ONE tokscale-compatible JSON object
  per line (``input_tokens`` / ``output_tokens`` /
  ``cache_creation_input_tokens`` / ``cache_read_input_tokens`` / ``model`` /
  ``timestamp`` / ``session_id``).
* :func:`to_daily_rollup` — a tokscale-compatible per-day rollup (day is the
  LOCAL-timezone ``%Y-%m-%d``) for atelier feature-2.
* :func:`aggregate` — saturating per-group token sums over a ``group_by`` subset
  of ``{run, cycle, phase, role, model, day}``.

The four token categories are NEVER collapsed into one token total — the rollups
and aggregates keep all four columns split. Stdlib-only; this module renders DATA,
it never interprets report content as instructions.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping
from datetime import datetime

from scripts.tokenmeter_schema import CATEGORY_FIELDS, NA, SATURATION_MAX

#: Group-by axes supported by :func:`aggregate`.
GROUP_KEYS = ("run", "cycle", "phase", "role", "model", "day")


# ── shared accessors ─────────────────────────────────────────────────────────


def _get(obj, name, default=None):
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _usage_field(rec, name):
    usage = _get(rec, "usage")
    raw = _get(usage if usage is not None else rec, name, 0)
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


def _figure_mean(value):
    if isinstance(value, Mapping):
        return value.get("mean")
    return value


def _local_day(timestamp):
    """LOCAL-timezone ``%Y-%m-%d`` for an ISO string or epoch; ``unknown`` if unparseable."""
    if timestamp is None:
        return "unknown"
    try:
        if isinstance(timestamp, int | float) and not isinstance(timestamp, bool):
            moment = datetime.fromtimestamp(timestamp).astimezone()
        else:
            moment = datetime.fromisoformat(str(timestamp)).astimezone()
        return moment.strftime("%Y-%m-%d")
    except (ValueError, OverflowError, OSError):
        return "unknown"


def _saturate(value):
    return value if value < SATURATION_MAX else SATURATION_MAX


# ── render_json ──────────────────────────────────────────────────────────────


def render_json(report):
    """Canonical (deterministic) JSON serialization of a report."""
    return json.dumps(report, indent=2, sort_keys=True, default=str)


# ── render_markdown ──────────────────────────────────────────────────────────


def _fmt(value):
    if value is None:
        return NA
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        if value == 0:
            return "0"
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def _cell(figure):
    """Render a before/after figure cell (mean + dispersion for dynamic)."""
    mean = _figure_mean(figure)
    if mean is None:
        return NA
    if isinstance(figure, Mapping):
        cv = figure.get("cv")
        cv_text = NA if cv is None else f"{cv:.3f}"
        return f"{_fmt(mean)} (n={figure.get('n')},cv={cv_text},{figure.get('confidence')})"
    return _fmt(mean)


def _pct(value):
    if value is None:
        return NA
    return f"{value:+.1f}%"


def render_markdown(report):
    """Render a report as a Markdown table with an outcome footer."""
    md = report.get("metadata", {})
    lines = [
        f"# Tokenmeter — {md.get('target', '')}",
        "",
        (
            f"model=`{md.get('model')}` effort=`{md.get('effort')}` "
            f"transport=`{md.get('transport')}` cycles=`{md.get('cycles')}` "
            f"n_runs=`{md.get('n_runs')}` rate_table_as_of=`{md.get('rate_table_as_of')}`"
        ),
        "",
        "| row | kind | mode | source | suspect | BEFORE | AFTER | delta_abs | delta_pct |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in report.get("rows", []):
        before = _cell(row.get("before"))
        after = _cell(row.get("after"))
        delta_abs = _fmt(_figure_mean(row.get("delta_abs")))
        delta_pct = _pct(row.get("delta_pct"))
        lines.append(
            f"| {row.get('row')} | {row.get('kind')} | {row.get('mode')} | "
            f"{row.get('source')} | {row.get('suspect')} | {before} | {after} | "
            f"{delta_abs} | {delta_pct} |"
        )

    lines += ["", "## Derived", "| row | BEFORE | AFTER | source |", "| --- | --- | --- | --- |"]
    for entry in report.get("derived", []):
        lines.append(
            f"| {entry.get('row')} | {_fmt(_figure_mean(entry.get('before')))} | "
            f"{_fmt(_figure_mean(entry.get('after')))} | {entry.get('source')} |"
        )

    outcome = report.get("outcome", {})
    cost = report.get("cost_oracle", {})
    lines += [
        "",
        (
            "**Outcome:** "
            f"cycles_succeeded={outcome.get('cycles_succeeded')} · "
            f"cycles_abandoned={outcome.get('cycles_abandoned')} · "
            f"pr_opened={outcome.get('pr_opened')} · "
            f"tests_green={outcome.get('tests_green')}"
        ),
        (
            "**Cost oracle:** "
            f"seam_a={cost.get('seam_a_total_cost_usd')} · "
            f"computed={cost.get('computed_total_cost_usd')} · "
            f"reconciled={cost.get('reconciled')} · "
            f"divergence_cause={cost.get('divergence_cause')}"
        ),
    ]
    return "\n".join(lines)


# ── render_csv ───────────────────────────────────────────────────────────────


def _scalar(value):
    if value is None:
        return ""
    if isinstance(value, Mapping):
        return value.get("mean")
    return value


def render_csv(report):
    """Render a report's rows as CSV (one row per report row)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["row", "kind", "mode", "source", "suspect", "before", "after", "delta_abs", "delta_pct"]
    )
    for row in report.get("rows", []):
        writer.writerow(
            [
                row.get("row"),
                row.get("kind"),
                row.get("mode"),
                row.get("source"),
                row.get("suspect"),
                _scalar(row.get("before")),
                _scalar(row.get("after")),
                _scalar(row.get("delta_abs")),
                _scalar(row.get("delta_pct")),
            ]
        )
    return buffer.getvalue()


# ── render_jsonl (per-record evidence) ───────────────────────────────────────


def render_jsonl(records, *, default_model=None):
    """Emit ONE tokscale-compatible JSON object per record (newline-joined)."""
    lines = []
    for rec in records:
        usage = _get(rec, "usage", rec)
        evidence = {
            "input_tokens": _usage_field(usage, "input_tokens"),
            "output_tokens": _usage_field(usage, "output_tokens"),
            "cache_creation_input_tokens": _usage_field(usage, "cache_creation_input_tokens"),
            "cache_read_input_tokens": _usage_field(usage, "cache_read_input_tokens"),
            "model": _get(rec, "model") or default_model,
            "timestamp": _get(rec, "timestamp"),
            "session_id": _get(rec, "session_id"),
        }
        lines.append(json.dumps(evidence, sort_keys=True, default=str))
    return "\n".join(lines)


# ── to_daily_rollup (tokscale-compatible) ────────────────────────────────────


def to_daily_rollup(records, *, default_model=None):
    """Roll records up per (LOCAL day, model) with the four categories kept split."""
    buckets: dict = {}
    for rec in records:
        key = (_local_day(_get(rec, "timestamp")), _get(rec, "model") or default_model or "")
        bucket = buckets.setdefault(key, dict.fromkeys(CATEGORY_FIELDS, 0))
        usage = _get(rec, "usage", rec)
        for field in CATEGORY_FIELDS:
            bucket[field] = _saturate(bucket[field] + _usage_field(usage, field))

    rollup = []
    for day, model in sorted(buckets, key=lambda k: (str(k[0]), str(k[1]))):
        entry = {"day": day, "model": model}
        entry.update(buckets[(day, model)])
        rollup.append(entry)
    return rollup


# ── aggregate ────────────────────────────────────────────────────────────────


def _group_value(rec, key):
    if key == "day":
        return _local_day(_get(rec, "timestamp"))
    if key == "role":
        return _get(rec, "agent_label")
    return _get(rec, key)


def aggregate(records, group_by):
    """Saturating per-group token sums over a ``group_by`` subset of :data:`GROUP_KEYS`.

    The four categories stay split (never collapsed into one total). Sums are
    saturating: a value can never exceed :data:`SATURATION_MAX`.
    """
    keys = list(group_by)
    invalid = [key for key in keys if key not in GROUP_KEYS]
    if invalid:
        raise ValueError(f"unsupported group_by keys: {invalid} (allowed: {list(GROUP_KEYS)})")

    buckets: dict = {}
    for rec in records:
        group_key = tuple(_group_value(rec, key) for key in keys)
        bucket = buckets.setdefault(group_key, dict.fromkeys(CATEGORY_FIELDS, 0))
        usage = _get(rec, "usage", rec)
        for field in CATEGORY_FIELDS:
            bucket[field] = _saturate(bucket[field] + _usage_field(usage, field))

    out = []
    for group_key in sorted(buckets, key=lambda k: tuple(str(part) for part in k)):
        entry = dict(zip(keys, group_key, strict=False))
        entry.update(buckets[group_key])
        out.append(entry)
    return out
