"""tokenmeter CLI — the user-facing front door for kaizen's token-usage benchmark.

Composes the tokenmeter modules (collect -> price -> assemble -> render) behind
four subcommands, mirroring the :mod:`scripts.codegraph_recon` CLI shape
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
* ``benchmark <scenario.json>`` — the before/after harness. Loads a
  :class:`scripts.tokenmeter_scenario.Scenario`, replays its prompt N times via an
  injectable headless ``claude`` runner, and emits ONE static+dynamic
  ``BenchmarkReport`` (:func:`scripts.tokenmeter_run.benchmark_target`). With
  ``--out before.json`` then ``--out after.json`` around an improvement, the EXISTING
  ``report`` subcommand renders the delta (the control-vector gate guarantees the
  comparison is attributable to the edit, since both share the scenario_hash).
* ``report <before.json> <after.json>`` — deltas two previously-emitted reports into a
  ``BEFORE | AFTER | Δ`` view, REFUSING the delta (control-vector gate,
  :func:`scripts.tokenmeter_schema.assert_controls_match`) if model / effort /
  scenario / cycles / transport / rate-table drifted between the two.
* ``daily [--config-dir DIR] [--since YYYY-MM-DD]`` — the tokscale-compatible per-day
  token rollup (the atelier feature-2 feed). Walks the transcript root (``--config-dir``
  or the default ``$CLAUDE_CONFIG_DIR`` / ``~/.claude``,
  :func:`scripts.tokenmeter_transcript.collect_usage_records`), buckets the four token
  categories per LOCAL-tz day + model
  (:func:`scripts.tokenmeter_render.to_daily_rollup`), and emits JSON to stdout. Read-only
  — it never mutates the transcript tree.

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
from datetime import datetime
from pathlib import Path
from typing import Any

from scripts.tokenmeter_render import render_csv, render_json, render_markdown, to_daily_rollup
from scripts.tokenmeter_result import classify_result, parse_result
from scripts.tokenmeter_run import (
    DEFAULT_PERMISSION_MODE,
    PERMISSION_MODES,
    benchmark_target,
    real_claude_runner,
)
from scripts.tokenmeter_scenario import load_scenario
from scripts.tokenmeter_schema import assemble, assert_controls_match, derive_outcome_score
from scripts.tokenmeter_static import static_footprint
from scripts.tokenmeter_transcript import collect_usage_records

_FORMATS = ("json", "md", "csv")

#: The ``static`` / ``dynamic`` subcommands carry no run outcomes (cycles
#: succeeded/abandoned, PR opened) — those are a kaizen-run concern with no meaning
#: for a one-shot footprint/transcript read. ``assemble`` defaults every outcome
#: field, so an empty dict yields a valid report. The ``benchmark`` subcommand DOES
#: accept them (see :func:`_benchmark_outcomes` + the ``--cycles-succeeded`` etc.
#: flags) so its report honours design §5 "never report cost alone".
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


def _render(report: dict[str, Any], fmt: str) -> str:
    """Render an assembled (or delta) report to a string in the requested format."""
    if fmt == "md":
        return render_markdown(report)
    if fmt == "csv":
        return render_csv(report)
    return render_json(report)


def _emit_report(report: dict[str, Any], fmt: str) -> None:
    """Render an assembled (or delta) report in the requested format to stdout."""
    text = _render(report, fmt)
    if fmt == "csv":
        sys.stdout.write(text)
    else:
        print(text)


def _benchmark_metadata(args: argparse.Namespace) -> dict[str, Any]:
    """Build the caller-supplied metadata for ``benchmark`` (scenario fills the rest).

    Only the descriptors the scenario does NOT own are set here: ``model`` (pricing
    + control vector), ``transport`` / ``effort`` (control vector), and
    ``target_commit`` (the before/after VERSION discriminator — NOT a control, so it
    is expected to differ across a delta). ``target`` / ``subject`` / ``cycles``
    override the scenario only when explicitly supplied.
    """
    md: dict[str, Any] = {
        "model": args.model or "",
        "transport": args.transport or "",
        "effort": args.effort or "",
    }
    if args.target:
        md["target"] = args.target
    if args.subject:
        md["subject"] = args.subject
    if args.cycles:
        md["cycles"] = args.cycles
    if getattr(args, "target_commit", None):
        md["target_commit"] = args.target_commit
    return md


def _benchmark_outcomes(args: argparse.Namespace) -> dict[str, Any]:
    """Build the run-outcome dict for ``benchmark`` from the CLI flags.

    Design §5 "never report cost alone" pairs the token figures with the run's
    outcome (cycles succeeded/abandoned, PR opened, tests green) — the
    tokens-to-green anchor. Without these flags the outcome footer was always
    all-zero; here the operator supplies them so a token win that abandoned more
    cycles or shipped worse is visible in the same report.

    OckScore (design §5) is an OPTIONAL composite that only surfaces when an
    ``outcome_score`` is present. We honour an explicit ``--outcome-score`` and
    otherwise DERIVE a ``0..1`` score from the anchors above (``--tests-green``
    scaled by the cycle-success ratio — see
    :func:`scripts.tokenmeter_schema.derive_outcome_score`), so the
    ``ockscore_optional_composite`` row appears on a normal run that carries outcome
    info and stays absent when it carries none. Without this the row was unreachable
    from the CLI (the feature was dead in prod).
    """
    outcomes: dict[str, Any] = {
        "cycles_succeeded": args.cycles_succeeded,
        "cycles_abandoned": args.cycles_abandoned,
        "pr_opened": args.pr_opened,
        "tests_green": args.tests_green,
    }
    if args.outcome_score is not None:
        outcomes["outcome_score"] = args.outcome_score
    else:
        derived = derive_outcome_score(outcomes)
        if derived is not None:
            outcomes["outcome_score"] = derived
    return outcomes


def _warn_failed_runs(report: dict[str, Any]) -> None:
    """Print a fail-loud stderr summary if any dynamic run FAILED (design §4).

    The report's ``runs`` block already carries the failure marker; this surfaces it
    on stderr so an operator reading the terminal sees the broken run immediately,
    not just whoever later parses the JSON.
    """
    runs = report.get("runs", {})
    if runs.get("any_failed"):
        print(
            f"[tokenmeter] WARNING: {runs.get('runs_failed')}/{runs.get('n_runs')} "
            f"benchmark run(s) FAILED — statuses={runs.get('statuses')}; the report's "
            "measured rows reflect ONLY the runs that produced a transcript",
            file=sys.stderr,
        )


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


def _validate_since(value: str) -> str:
    """Validate a ``--since`` value is a ``YYYY-MM-DD`` date; raise ``ValueError`` else."""
    try:
        datetime.strptime(value, "%Y-%m-%d")  # a date-only key, never a wall-clock moment
    except ValueError as exc:
        raise ValueError(f"--since must be YYYY-MM-DD, got {value!r}") from exc
    return value


def cmd_daily(args: argparse.Namespace) -> int:
    """``daily`` — tokscale-compatible per-day token rollup (atelier feature-2 feed).

    Harvests :func:`scripts.tokenmeter_transcript.collect_usage_records` over the
    ``--config-dir`` transcript root (or the default ``$CLAUDE_CONFIG_DIR`` / ``~/.claude``
    when omitted) and emits :func:`scripts.tokenmeter_render.to_daily_rollup` — one bucket
    per (LOCAL-tz ``%Y-%m-%d``, model) with the four token categories kept SPLIT, using the
    tokscale-compatible field names. ``--since YYYY-MM-DD`` keeps only days on
    or after that date (``unknown``-day buckets — records with no/unparseable timestamp —
    are retained, since they cannot be proven to predate the cutoff). JSON to stdout; the
    walk is read-only and never mutates the transcript tree.
    """
    records = collect_usage_records(config_dir=args.config_dir)
    rollup = to_daily_rollup(records)
    if args.since:
        since = _validate_since(args.since)
        rollup = [
            entry
            for entry in rollup
            if entry.get("day") == "unknown" or str(entry.get("day")) >= since
        ]
    print(json.dumps(rollup, indent=2, sort_keys=True, default=str))
    return 0


def cmd_benchmark(args: argparse.Namespace, *, runner: Any = None) -> int:
    """``benchmark`` — static+dynamic report for a scenario (the before/after harness).

    ``runner`` is the INJECTABLE headless-``claude`` runner; tests pass a fake so
    no real ``claude`` is spawned. When ``None`` the default
    :func:`scripts.tokenmeter_run.real_claude_runner` shells the real binary. With
    ``--out`` the report is written to a file (so ``before.json`` / ``after.json``
    can feed the ``report`` delta); otherwise it is printed to stdout.
    """
    scenario = load_scenario(args.scenario)
    eff_runner = runner if runner is not None else real_claude_runner
    report = benchmark_target(
        scenario,
        n=args.n,
        runner=eff_runner,
        cwd=args.cwd,
        config_dir=args.config_dir,
        cycle=args.cycle,
        repo_root=args.repo_root,
        metadata=_benchmark_metadata(args),
        outcomes=_benchmark_outcomes(args),
        permission_mode=args.permission_mode,
        evidence_out=args.evidence_out,
        rollup_out=args.rollup_out,
    )
    _warn_failed_runs(report)
    text = _render(report, args.format)
    if args.out:
        body = text if text.endswith("\n") else text + "\n"
        Path(args.out).write_text(body, encoding="utf-8")
        print(
            f"[tokenmeter] benchmark report ({args.format}) written to {args.out}", file=sys.stderr
        )
    elif args.format == "csv":
        sys.stdout.write(text)
    else:
        print(text)
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


def main(argv: list[str], *, runner: Any = None) -> int:
    """CLI entry. stdout = canonical output; diagnostics + errors → stderr.

    ``runner`` (keyword-only) is the injectable headless-``claude`` runner threaded
    into the ``benchmark`` subcommand; tests pass a fake so no real ``claude`` is
    spawned. It is ignored by the other subcommands. On any error a single
    ``{"status": "error", "reason": ...}`` line is printed to stdout and 1 is
    returned (the codegraph_recon idiom).
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

    p_benchmark = sub.add_parser(
        "benchmark",
        help="static+dynamic report for a scenario JSON (the before/after harness)",
    )
    p_benchmark.add_argument("scenario", help="path to a benchmark/scenarios/<name>.json")
    p_benchmark.add_argument("--format", choices=_FORMATS, default="json")
    p_benchmark.add_argument(
        "--out", default=None, help="write the report to this file (e.g. before.json)"
    )
    p_benchmark.add_argument("--n", type=int, default=3, help="number of dynamic runs (default 3)")
    p_benchmark.add_argument("--cycle", default=None, help="kaizen cycle label to tag records with")
    p_benchmark.add_argument(
        "--cwd", default=None, help="working dir the headless claude runs in (default: process CWD)"
    )
    p_benchmark.add_argument(
        "--config-dir",
        default=None,
        help=(
            "OPTIONAL seeded-credentials $CLAUDE_CONFIG_DIR override; by DEFAULT the "
            "harness does NOT relocate it (so subscription auth works) and scopes the "
            "harvest by session_id instead"
        ),
    )
    p_benchmark.add_argument(
        "--repo-root",
        default=None,
        help="root the scenario target path resolves against (default: CWD)",
    )
    p_benchmark.add_argument(
        "--target-commit", default=None, help="target version/commit (before/after discriminator)"
    )
    p_benchmark.add_argument(
        "--permission-mode",
        choices=PERMISSION_MODES,
        default=DEFAULT_PERMISSION_MODE,
        help=f"headless claude permission mode (default {DEFAULT_PERMISSION_MODE})",
    )
    p_benchmark.add_argument(
        "--evidence-out",
        default=None,
        help="write the per-call Seam-B JSONL evidence (§5) to this path",
    )
    p_benchmark.add_argument(
        "--rollup-out",
        default=None,
        help="write the tokscale-compatible daily-rollup (§7) JSON to this path",
    )
    # Run-outcome flags (design §5 — never report cost alone). Thread into the
    # report's outcome footer; absent flags default to 0 / False as before.
    p_benchmark.add_argument(
        "--cycles-succeeded", type=int, default=0, help="cycles that succeeded this run"
    )
    p_benchmark.add_argument(
        "--cycles-abandoned", type=int, default=0, help="cycles that were abandoned this run"
    )
    p_benchmark.add_argument(
        "--pr-opened", action="store_true", help="mark that a PR was opened for this run"
    )
    p_benchmark.add_argument(
        "--tests-green", action="store_true", help="mark that the target's tests were green"
    )
    p_benchmark.add_argument(
        "--outcome-score",
        type=float,
        default=None,
        help=(
            "explicit 0..1 unit-of-work outcome for the OPTIONAL OckScore composite "
            "(design §5); when omitted it is DERIVED from --tests-green + the cycle-success "
            "ratio so the ockscore row still appears on a normal run with outcome info"
        ),
    )
    _add_meta_args(p_benchmark)

    p_report = sub.add_parser("report", help="delta two emitted reports (BEFORE | AFTER | delta)")
    p_report.add_argument("before", help="baseline report JSON")
    p_report.add_argument("after", help="improved report JSON")
    p_report.add_argument("--format", choices=_FORMATS, default="json")

    p_daily = sub.add_parser(
        "daily",
        help="tokscale-compatible per-day token rollup (the atelier feature-2 feed)",
    )
    p_daily.add_argument(
        "--config-dir",
        default=None,
        help="transcript root to walk ($CLAUDE_CONFIG_DIR); default ~/.claude",
    )
    p_daily.add_argument(
        "--since",
        default=None,
        help="keep only LOCAL-tz days on or after this YYYY-MM-DD",
    )

    args = parser.parse_args(argv)

    try:
        if args.cmd == "static":
            return cmd_static(args)
        if args.cmd == "dynamic":
            return cmd_dynamic(args)
        if args.cmd == "benchmark":
            return cmd_benchmark(args, runner=runner)
        if args.cmd == "report":
            return cmd_report(args)
        if args.cmd == "daily":
            return cmd_daily(args)
        return 2  # unreachable: required subparser
    except Exception as exc:
        print(json.dumps({"status": "error", "reason": str(exc)}))
        print(f"[tokenmeter] {args.cmd} failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
