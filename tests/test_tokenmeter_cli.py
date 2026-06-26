"""Tests for the tokenmeter CLI (scripts.tokenmeter) driving ``main(argv)`` directly.

ALL fixtures are built INLINE under ``tmp_path`` — a synthetic plugin tree, synthetic
JSONL transcript trees, and synthetic result envelopes. No real ``claude`` is ever
spawned and there are NO checked-in fixture files. stdout is captured via ``capsys``.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path

import pytest

from scripts.tokenmeter import main
from scripts.tokenmeter_pricing import cost_usd

MODEL = "claude-opus-4-7"
REPO_ROOT = Path(__file__).resolve().parent.parent


# ── inline fixture builders ──────────────────────────────────────────────────


def _make_plugin(root):
    """Minimal plugin tree → the skill dir (SKILL.md + a .claude-plugin marker)."""
    skill_dir = root / "skills" / "improve"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\ndescription: demo skill for the tokenmeter CLI test\n---\n\n# improve\n\nBody.\n",
        encoding="utf-8",
    )
    (root / ".claude-plugin").mkdir()
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "demo", "version": "0.1.0"}), encoding="utf-8"
    )
    return skill_dir


def _make_transcript(root, *, input_tokens, output_tokens):
    """One transcript root with a single orchestrator assistant line."""
    proj = root / "projects" / "proj"
    proj.mkdir(parents=True)
    line = json.dumps(
        {
            "type": "assistant",
            "sessionId": "sess-main",
            "message": {
                "id": "m1",
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        }
    )
    (proj / "session.jsonl").write_text(line + "\n", encoding="utf-8")
    return root


def _write_result(path, *, total_cost_usd, input_tokens=1000, output_tokens=500):
    path.write_text(
        json.dumps(
            {
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
                "total_cost_usd": total_cost_usd,
                "is_error": False,
                "session_id": "s1",
                "num_turns": 1,
                "duration_ms": 10,
            }
        ),
        encoding="utf-8",
    )
    return path


# ── static ───────────────────────────────────────────────────────────────────


def test_static_json_is_the_footprint(tmp_path, capsys):
    skill_dir = _make_plugin(tmp_path)
    assert main(["static", str(skill_dir)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "static"
    assert out["files"]  # at least the passive description + active-core body


def test_static_md_renders_a_table(tmp_path, capsys):
    skill_dir = _make_plugin(tmp_path)
    assert main(["static", str(skill_dir), "--format", "md"]) == 0
    out = capsys.readouterr().out
    assert "Tokenmeter" in out
    assert "| row |" in out


def test_static_csv_has_header(tmp_path, capsys):
    skill_dir = _make_plugin(tmp_path)
    assert main(["static", str(skill_dir), "--format", "csv"]) == 0
    assert capsys.readouterr().out.startswith("row,kind,mode")


def test_static_report_omits_phantom_dynamic_category_rows(tmp_path, capsys):
    # Finding G: a static-only report has NO dynamic records, so the four category
    # rows (which used to render all-zero, mislabeled mode=dynamic/source=measured)
    # must be SUPPRESSED — only the static overhead rows remain.
    skill_dir = _make_plugin(tmp_path)

    assert main(["static", str(skill_dir), "--format", "md"]) == 0
    md = capsys.readouterr().out
    for category in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        assert f"| {category} | category | dynamic" not in md
    assert "| overhead | static |" in md  # static overhead rows are still emitted

    assert main(["static", str(skill_dir), "--format", "csv"]) == 0
    csv_out = capsys.readouterr().out
    assert ",category,dynamic,measured," not in csv_out
    assert ",overhead,static," in csv_out


# ── dynamic (transcript root) ────────────────────────────────────────────────


def test_dynamic_transcript_root_category_rows(tmp_path, capsys):
    root = _make_transcript(tmp_path / "tree", input_tokens=42, output_tokens=7)
    assert main(["dynamic", str(root), "--model", MODEL]) == 0
    report = json.loads(capsys.readouterr().out)
    rows = {(r["kind"], r["row"]): r for r in report["rows"]}
    assert rows[("category", "input_tokens")]["after"]["mean"] == 42
    assert rows[("category", "output_tokens")]["after"]["mean"] == 7


# ── dynamic (result json) ────────────────────────────────────────────────────


def test_dynamic_result_json_reconciles_rate_math(tmp_path, capsys):
    # opus-4-7: 1000/1e6*5.0 + 500/1e6*25.0 == 0.0175 → computed == oracle → agree.
    result_path = _write_result(tmp_path / "result.json", total_cost_usd=0.0175)
    assert main(["dynamic", str(result_path), "--model", MODEL]) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    co = report["cost_oracle"]
    assert co["seam_a_total_cost_usd"] == 0.0175
    assert co["reconciled"] == "agree"
    # The run-status note rides on stderr, never polluting the canonical stdout JSON.
    assert "run status: success" in captured.err


# ── report (delta) ───────────────────────────────────────────────────────────


def test_report_deltas_two_reports(tmp_path, capsys):
    before_root = _make_transcript(tmp_path / "b", input_tokens=1000, output_tokens=500)
    after_root = _make_transcript(tmp_path / "a", input_tokens=600, output_tokens=300)

    assert main(["dynamic", str(before_root), "--model", MODEL]) == 0
    before_out = capsys.readouterr().out
    assert main(["dynamic", str(after_root), "--model", MODEL]) == 0
    after_out = capsys.readouterr().out

    before_json = tmp_path / "before.json"
    before_json.write_text(before_out, encoding="utf-8")
    after_json = tmp_path / "after.json"
    after_json.write_text(after_out, encoding="utf-8")

    assert main(["report", str(before_json), str(after_json)]) == 0
    delta = json.loads(capsys.readouterr().out)
    rows = {(r["kind"], r["row"]): r for r in delta["rows"]}
    input_row = rows[("category", "input_tokens")]
    assert input_row["before"]["mean"] == 1000
    assert input_row["after"]["mean"] == 600
    assert input_row["delta_abs"] == -400  # leaner by 400 input tokens


def test_report_refuses_control_drift(tmp_path, capsys):
    root = _make_transcript(tmp_path / "r", input_tokens=10, output_tokens=5)

    assert main(["dynamic", str(root), "--model", "claude-opus-4-7"]) == 0
    out_a = capsys.readouterr().out
    # Different model → the control vector drifts → the delta MUST be refused.
    assert main(["dynamic", str(root), "--model", "claude-sonnet-4-6"]) == 0
    out_b = capsys.readouterr().out

    fa = tmp_path / "a.json"
    fa.write_text(out_a, encoding="utf-8")
    fb = tmp_path / "b.json"
    fb.write_text(out_b, encoding="utf-8")

    rc = main(["report", str(fa), str(fb)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "control vector" in out  # the drift reason rides on the error JSON


# ── error handling ───────────────────────────────────────────────────────────


def test_dynamic_missing_path_is_clean_error(tmp_path, capsys):
    rc = main(["dynamic", str(tmp_path / "does-not-exist")])
    out = capsys.readouterr().out
    assert rc == 1
    assert '"status": "error"' in out


def test_missing_subcommand_exits(capsys):
    with pytest.raises(SystemExit):
        main([])


# ── daily rollup (tokscale feature-2 feed) ───────────────────────────────────

_TOKSCALE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


@contextlib.contextmanager
def _forced_tz(tz):
    """Force the LOCAL timezone for the block (deterministic local-day bucketing)."""
    if not hasattr(time, "tzset"):  # pragma: no cover - platform guard
        pytest.skip("time.tzset unavailable on this platform")
    prev = os.environ.get("TZ")
    os.environ["TZ"] = tz
    time.tzset()
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = prev
        time.tzset()


def _timed_line(message_id, timestamp, *, input_tokens, output_tokens, model=MODEL):
    return json.dumps(
        {
            "type": "assistant",
            "sessionId": "sess-main",
            "timestamp": timestamp,
            "message": {
                "id": message_id,
                "model": model,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        }
    )


def _make_timed_transcript(root, lines):
    proj = root / "projects" / "proj"
    proj.mkdir(parents=True)
    (proj / "session.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


def test_daily_buckets_per_local_day_with_tokscale_fields(tmp_path, capsys):
    # TZ = UTC+5 (POSIX "XXX-5", no DST). The two lines share a UTC day (2026-06-25) but
    # fall on DIFFERENT local days — proving LOCAL-tz bucketing, not UTC.
    with _forced_tz("XXX-5"):
        root = _make_timed_transcript(
            tmp_path / "tree",
            [
                _timed_line("m1", "2026-06-25T10:00:00Z", input_tokens=100, output_tokens=10),
                _timed_line("m2", "2026-06-25T20:00:00Z", input_tokens=200, output_tokens=20),
            ],
        )
        assert main(["daily", "--config-dir", str(root)]) == 0
        rollup = json.loads(capsys.readouterr().out)

    by_day = {e["day"]: e for e in rollup}
    # Local-tz split: 10:00Z → 15:00 (2026-06-25); 20:00Z → 01:00 next day (2026-06-26).
    assert set(by_day) == {"2026-06-25", "2026-06-26"}
    assert "2026-06-25" != "2026-06-26"  # UTC would have collapsed these into one bucket
    for entry in rollup:
        assert all(field in entry for field in _TOKSCALE_FIELDS)  # tokscale-named aggregates
        assert entry["model"] == MODEL
    assert by_day["2026-06-25"]["input_tokens"] == 100
    assert by_day["2026-06-25"]["output_tokens"] == 10
    assert by_day["2026-06-26"]["input_tokens"] == 200
    assert by_day["2026-06-26"]["output_tokens"] == 20


def test_daily_since_filters_earlier_days(tmp_path, capsys):
    with _forced_tz("XXX-5"):
        root = _make_timed_transcript(
            tmp_path / "tree",
            [
                _timed_line("m1", "2026-06-25T10:00:00Z", input_tokens=100, output_tokens=10),
                _timed_line("m2", "2026-06-25T20:00:00Z", input_tokens=200, output_tokens=20),
            ],
        )
        assert main(["daily", "--config-dir", str(root), "--since", "2026-06-26"]) == 0
        rollup = json.loads(capsys.readouterr().out)

    assert [e["day"] for e in rollup] == ["2026-06-26"]  # the earlier bucket is dropped
    assert rollup[0]["input_tokens"] == 200


def test_daily_aggregates_same_day_lines(tmp_path, capsys):
    # Two lines at the SAME timestamp → ONE bucket with summed tokscale fields.
    root = _make_timed_transcript(
        tmp_path / "tree",
        [
            _timed_line("m1", "2026-06-25T10:00:00Z", input_tokens=100, output_tokens=10),
            _timed_line("m2", "2026-06-25T10:00:00Z", input_tokens=400, output_tokens=40),
        ],
    )
    assert main(["daily", "--config-dir", str(root)]) == 0
    rollup = json.loads(capsys.readouterr().out)
    assert len(rollup) == 1
    assert rollup[0]["input_tokens"] == 500  # 100 + 400 summed in one day bucket
    assert rollup[0]["output_tokens"] == 50


def test_daily_empty_root_is_empty_list(tmp_path, capsys):
    assert main(["daily", "--config-dir", str(tmp_path / "nonexistent")]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_daily_bad_since_is_clean_error(tmp_path, capsys):
    root = _make_timed_transcript(
        tmp_path / "tree",
        [_timed_line("m1", "2026-06-25T10:00:00Z", input_tokens=1, output_tokens=1)],
    )
    rc = main(["daily", "--config-dir", str(root), "--since", "25-06-2026"])
    out = capsys.readouterr().out
    assert rc == 1
    assert '"status": "error"' in out
    assert "YYYY-MM-DD" in out


# ── benchmark: OckScore reachable from the CLI (MAJOR fix) ────────────────────


class _BenchRunner:
    """Minimal injected benchmark runner: writes one orchestrator transcript line per
    run under ``$CLAUDE_CONFIG_DIR`` and returns a matching result envelope. No real
    ``claude`` is spawned; ``total_cost_usd`` is priced to AGREE with the transcript so
    the run classifies SUCCESS (never a $0 failure)."""

    is_fake = True

    def __init__(self, per_run_usage):
        self._runs = per_run_usage
        self._i = 0

    async def __call__(self, argv, cwd):
        cfg = Path(os.environ["CLAUDE_CONFIG_DIR"])
        usage = self._runs[self._i]
        self._i += 1
        session_id = f"sess-{self._i}"
        sess = cfg / "projects" / "proj" / session_id
        sess.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "type": "assistant",
                "sessionId": session_id,
                "timestamp": "2026-06-25T12:00:00Z",
                "message": {"id": f"m{self._i}", "model": MODEL, "usage": usage},
            }
        )
        (sess / "session.jsonl").write_text(line + "\n", encoding="utf-8")
        return json.dumps(
            {
                "total_cost_usd": cost_usd(usage, MODEL).total_cost,
                "usage": usage,
                "is_error": False,
                "session_id": session_id,
                "num_turns": 1,
            }
        )


def _bench_scenario_file(tmp_path):
    scn = tmp_path / "scn.json"
    scn.write_text(
        json.dumps(
            {
                "name": "bench",
                "target": "skills/improve",
                "prompt": "exercise the target on a representative task",
                "source": "user",
                "cycles": 1,
                "subject": "leaner target",
            }
        ),
        encoding="utf-8",
    )
    return scn


def _run_benchmark(tmp_path, monkeypatch, *, flags, n=1):
    """Drive ``main(["benchmark", ...])`` with the inline fake runner → parsed report."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    scn = _bench_scenario_file(tmp_path)
    out = tmp_path / "report.json"
    usage = {
        "input_tokens": 1000,
        "output_tokens": 500,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 200,
    }
    argv = [
        "benchmark",
        str(scn),
        "--model",
        MODEL,
        "--n",
        str(n),
        "--repo-root",
        str(REPO_ROOT),
        *flags,
        "--out",
        str(out),
    ]
    assert main(argv, runner=_BenchRunner([usage] * n)) == 0
    return json.loads(out.read_text())


def test_benchmark_emits_ockscore_row_from_derived_outcome(tmp_path, monkeypatch):
    """MAJOR fix: with outcome info (--tests-green + --cycles-succeeded) the CLI DERIVES
    an outcome_score, so the OPTIONAL ockscore row now appears on a real benchmark run."""
    report = _run_benchmark(
        tmp_path, monkeypatch, flags=["--tests-green", "--cycles-succeeded", "3"], n=2
    )
    derived = {d["row"]: d for d in report["derived"]}
    assert "ockscore_optional_composite" in derived
    ock = derived["ockscore_optional_composite"]
    assert ock["optional"] is True
    assert ock["source"] == "approximated"
    assert ock["outcome_score"] == 1.0  # tests_green (1.0) * 3/3 cycle-success ratio
    # the raw four-category figures still stand alongside it (never replaced)
    cats = {r["row"] for r in report["rows"] if r["kind"] == "category"}
    assert cats == {
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    }


def test_benchmark_emits_ockscore_row_from_explicit_outcome_score(tmp_path, monkeypatch):
    """An explicit --outcome-score also reaches the row (and wins over the derivation)."""
    report = _run_benchmark(tmp_path, monkeypatch, flags=["--outcome-score", "0.9"], n=1)
    derived = {d["row"]: d for d in report["derived"]}
    assert "ockscore_optional_composite" in derived
    assert derived["ockscore_optional_composite"]["outcome_score"] == 0.9


def test_benchmark_omits_ockscore_row_with_no_outcome_info(tmp_path, monkeypatch):
    """No outcome flags at all → no score is derived → the OPTIONAL row stays absent."""
    report = _run_benchmark(tmp_path, monkeypatch, flags=[], n=1)
    assert "ockscore_optional_composite" not in {d["row"] for d in report["derived"]}
