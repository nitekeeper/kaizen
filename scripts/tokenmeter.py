"""tokenmeter CLI — the user-facing front door for kaizen's token-usage benchmark.

Composes the six tokenmeter modules (collect -> price -> assemble -> render) behind
three subcommands, mirroring the :mod:`scripts.codegraph_recon` CLI shape
(``def main(argv) -> int`` + argparse sub-parsers + a ``sys.exit(main(...))`` guard):

* ``static <skill_dir>`` — the deterministic static footprint of a skill/plugin
  (:func:`scripts.tokenmeter_static.static_footprint`). Canonical JSON is the rich
  per-file / per-tier footprint; ``--format md|csv`` renders the assembled report's
  overhead rows.
* ``dynamic <transcript-root | result-json>`` — real runtime usage. A *directory* is
  walked as a Claude Code transcript root (Seam B ground truth,
  :func:`scripts.tokenmeter_transcript.collect_usage_records`); a *file* is parsed as
  a ``claude --output-format json`` result envelope (Seam A cost oracle,
  :func:`scripts.tokenmeter_result.parse_result`) and validates our rate math against
  the CLI's own ``total_cost_usd``.
* ``report <before.json> <after.json>`` — deltas two previously-emitted reports into a
  ``BEFORE | AFTER | Δ`` view, REFUSING the delta (control-vector gate,
  :func:`scripts.tokenmeter_schema.assert_controls_match`) if model / effort /
  scenario / cycles / transport / rate-table drifted between the two.

Output: canonical JSON to stdout by default; ``--format md|csv`` for the human table.
Diagnostics (run status, errors) go to stderr; on any error a single
``{"status": "error", ...}`` line is printed to stdout and the process returns 1
(the codegraph_recon idiom).

SECURITY: the skill files, transcripts, and result envelopes this CLI reads are
target-repo content — they are treated strictly as DATA (``json.loads`` only, no
``eval`` / ``exec`` / shell). Stdlib-only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from scripts.tokenmeter_render import render_csv, render_json, render_markdown
from scripts.tokenmeter_result import classify_result, parse_result
from scripts.tokenmeter_schema import assemble, assert_controls_match
from scripts.tokenmeter_static import static_footprint
from scripts.tokenmeter_transcript import collect_usage_records

_FORMATS = ("json", "md", "csv")

#: The CLI carries no run outcomes (cycles succeeded/abandoned, PR opened); those are
#: a kaizen-run concern. ``assemble`` defaults every outcome field, so an empty dict
#: yields a valid report.
_NO_OUTCOMES: dict[str, Any] = {}


# ── argument / metadata plumbing ─────────────────────────────────────────────


def _add_meta_args(parser: argparse.ArgumentParser) -> None:
    """Attach the shared run-descriptor flags that flow into report metadata."""
    parser.add_argument(
        "--model", default=None, help="canonical model id (pricing + control vector)"
    )
    parser.add_argument("--target", default=None, help="target id for the report header")
    parser.add_argument("--subject", default=None, help="improvement subject")
    parser.add_argument("--transport", default=None, help="transport label (e.g. host / bridge)")
    parser.add_argument("--effort", default=None, help="effort-level label")
    parser.add_argument("--cycles", type=int, default=0, help="number of improvement cycles")
    parser.add_argument(
        "--scenario-source", default=None, help="scenario source label (user / auto)"
    )


def _metadata(args: argparse.Namespace, default_target: str) -> dict[str, Any]:
    """Build the metadata sub-dict ``assemble`` needs from parsed args.

    Only the user-supplied descriptors are set here; ``assemble`` fills the rest
    (timestamp, local_tz, benchmark_version, rate_table_as_of, scenario_hash).
    """
    return {
        "target": args.target or default_target,
        "model": args.model or "",
        "subject": args.subject or "",
        "transport": args.transport or "",
        "effort": args.effort or "",
        "cycles": args.cycles,
        "scenario_source": args.scenario_source or "",
    }


def _emit_report(report: dict[str, Any], fmt: str) -> None:
    """Render an assembled (or delta) report in the requested format to stdout."""
    if fmt == "md":
        print(render_markdown(report))
    elif fmt == "csv":
        sys.stdout.write(render_csv(report))
    else:
        print(render_json(report))


# ── delta helpers (report subcommand) ────────────────────────────────────────


def _scalar(figure: Any) -> Any:
    """Comparison scalar of a figure (the ``mean`` of a dynamic aggregate, else itself)."""
    if isinstance(figure, dict):
        return figure.get("mean")
    return figure


def _delta(before: Any, after: Any) -> tuple[Any, Any]:
    """``(abs, pct)`` delta between two scalars; ``(None, None)`` if either is missing."""
    if before is None or after is None:
        return None, None
    delta_abs = after - before
    delta_pct = (delta_abs / before * 100.0) if before else None
    return delta_abs, delta_pct


def _delta_report(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Pair ``after`` rows with ``before`` rows (by kind+row) and recompute deltas.

    Returns a deep copy of ``after`` with each row's ``before`` cell filled from the
    matching ``before`` row's ``after`` figure and its ``delta_abs`` / ``delta_pct``
    recomputed. The control-vector gate is the caller's responsibility (run BEFORE
    this so a drifted delta is refused).
    """
    before_rows = {(r.get("kind"), r.get("row")): r.get("after") for r in before.get("rows", [])}
    before_derived = {d.get("row"): d.get("after") for d in before.get("derived", [])}
    # Reports are pure JSON, so a json round-trip is a safe, dependency-free deep copy.
    out = json.loads(json.dumps(after))
    for row in out.get("rows", []):
        prior = before_rows.get((row.get("kind"), row.get("row")))
        row["before"] = prior
        row["delta_abs"], row["delta_pct"] = _delta(_scalar(prior), _scalar(row.get("after")))
    for entry in out.get("derived", []):
        entry["before"] = before_derived.get(entry.get("row"))
    return out


def _load_report(path: str) -> dict[str, Any]:
    """Load a previously-emitted report JSON; raise ``ValueError`` if it is not an object."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON report object")
    return data


# ── subcommand handlers ──────────────────────────────────────────────────────


def cmd_static(args: argparse.Namespace) -> int:
    """``static`` — deterministic static footprint of a skill/plugin directory."""
    skill_dir = Path(args.skill_dir)
    footprint = static_footprint(skill_dir)
    if args.format == "json":
        print(render_json(footprint))
        return 0
    target = args.target or skill_dir.name or str(skill_dir)
    report = assemble([], footprint, outcomes=_NO_OUTCOMES, metadata=_metadata(args, target))
    _emit_report(report, args.format)
    return 0


def cmd_dynamic(args: argparse.Namespace) -> int:
    """``dynamic`` — runtime usage from a transcript root (dir) or a result json (file)."""
    source = Path(args.source)
    target = args.target or source.name or str(source)
    meta = _metadata(args, target)

    if source.is_dir():
        # Seam B ground truth: walk the on-disk transcripts under this config dir.
        records = collect_usage_records(config_dir=source)
        report = assemble(list(records), [], outcomes=_NO_OUTCOMES, metadata=meta)
    elif source.is_file():
        # Seam A cost oracle: a single `claude --output-format json` result envelope.
        raw = source.read_text(encoding="utf-8", errors="replace")
        result = parse_result(raw)  # raises ValueError on empty/unparseable (caught in main)
        status = classify_result(raw)
        print(f"[tokenmeter] run status: {status.value}", file=sys.stderr)
        # Surface the oracle's own usage as a single Seam-B-style record so the report
        # has a headline AND reconciles our rate math against the oracle's cost.
        record = {
            "usage": result.usage,
            "model": meta["model"] or None,
            "session_id": result.session_id,
        }
        report = assemble([record], [], outcomes=_NO_OUTCOMES, oracle=result, metadata=meta)
    else:
        raise FileNotFoundError(f"no such transcript root or result json: {source}")

    _emit_report(report, args.format)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """``report`` — delta two emitted reports (refuses across a drifted control vector)."""
    before = _load_report(args.before)
    after = _load_report(args.after)
    # CONTROL-VECTOR EQUALITY GATE — raises ControlDriftError if any control drifted.
    assert_controls_match(before.get("metadata", {}), after.get("metadata", {}))
    _emit_report(_delta_report(before, after), args.format)
    return 0


# ── CLI ──────────────────────────────────────────────────────────────────────


def main(argv: list[str]) -> int:
    """CLI entry. stdout = canonical output; diagnostics + errors → stderr.

    On any error a single ``{"status": "error", "reason": ...}`` line is printed to
    stdout and 1 is returned (the codegraph_recon idiom).
    """
    parser = argparse.ArgumentParser(
        prog="tokenmeter",
        description="Measure the token footprint of a kaizen target (static + dynamic).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_static = sub.add_parser("static", help="deterministic static footprint of a skill/plugin")
    p_static.add_argument(
        "skill_dir", help="skill directory containing SKILL.md (or a plugin root)"
    )
    p_static.add_argument("--format", choices=_FORMATS, default="json")
    _add_meta_args(p_static)

    p_dynamic = sub.add_parser(
        "dynamic", help="runtime usage from a transcript root (dir) or a result json (file)"
    )
    p_dynamic.add_argument("source", help="transcript root directory OR result-json file")
    p_dynamic.add_argument("--format", choices=_FORMATS, default="json")
    _add_meta_args(p_dynamic)

    p_report = sub.add_parser("report", help="delta two emitted reports (BEFORE | AFTER | delta)")
    p_report.add_argument("before", help="baseline report JSON")
    p_report.add_argument("after", help="improved report JSON")
    p_report.add_argument("--format", choices=_FORMATS, default="json")

    args = parser.parse_args(argv)

    try:
        if args.cmd == "static":
            return cmd_static(args)
        if args.cmd == "dynamic":
            return cmd_dynamic(args)
        if args.cmd == "report":
            return cmd_report(args)
        return 2  # unreachable: required subparser
    except Exception as exc:
        print(json.dumps({"status": "error", "reason": str(exc)}))
        print(f"[tokenmeter] {args.cmd} failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
