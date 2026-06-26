"""Tests for the tokenmeter CLI (scripts.tokenmeter) driving ``main(argv)`` directly.

ALL fixtures are built INLINE under ``tmp_path`` — a synthetic plugin tree, synthetic
JSONL transcript trees, and synthetic result envelopes. No real ``claude`` is ever
spawned and there are NO checked-in fixture files. stdout is captured via ``capsys``.
"""

from __future__ import annotations

import json

import pytest

from scripts.tokenmeter import main

MODEL = "claude-opus-4-7"


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
