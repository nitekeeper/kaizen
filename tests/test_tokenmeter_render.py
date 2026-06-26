"""Tests for the renderers + aggregation (scripts.tokenmeter_render).

All fixtures are inline dicts; no files are created.
"""

from __future__ import annotations

import json

import pytest

from scripts.tokenmeter_render import (
    aggregate,
    render_csv,
    render_json,
    render_jsonl,
    render_markdown,
    to_daily_rollup,
)
from scripts.tokenmeter_schema import assemble

MODEL = "claude-opus-4-7"


def _rec(**overrides):
    base = {
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 500,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 200,
        },
        "model": MODEL,
        "run": "run-1",
        "cycle": "c1",
        "phase": "implement",
        "agent_label": "backend-engineer-1",
        "is_sidechain": False,
        "kept_but_suspect": False,
        "session_id": "sess-1",
        "timestamp": "2026-06-25T10:00:00+00:00",
    }
    base.update(overrides)
    return base


def _meta(**overrides):
    base = {
        "target": "github.com/x/y",
        "target_commit": "abc",
        "transport": "cli",
        "model": MODEL,
        "effort": "high",
        "caching": True,
        "cycles": 3,
        "subject": "tokenmeter",
        "scenario_source": "scenario-v1",
        "claude_config_dir": "/home/u/.claude",
    }
    base.update(overrides)
    return base


def _outcomes():
    return {"cycles_succeeded": 2, "cycles_abandoned": 1, "pr_opened": True, "tests_green": True}


def _report():
    static_rows = [{"path": "skills/x/SKILL.md", "token_count": 320, "source": "approximated"}]
    return assemble([_rec()], static_rows, outcomes=_outcomes(), metadata=_meta())


# ── render_json ──────────────────────────────────────────────────────────────


def test_render_json_roundtrips_and_is_sorted():
    text = render_json(_report())
    parsed = json.loads(text)
    assert parsed["schema_version"]
    # canonical => sorted keys, so re-serializing the parsed obj is stable.
    assert render_json(parsed) == text


# ── render_markdown ──────────────────────────────────────────────────────────


def test_render_markdown_has_columns_and_footer():
    text = render_markdown(_report())
    assert "BEFORE | AFTER | delta_abs | delta_pct" in text
    assert "| source |" in text or "source |" in text
    assert "mode" in text
    assert "**Outcome:**" in text
    assert "cycles_succeeded=2" in text


def test_render_markdown_static_cells_show_na_marker():
    text = render_markdown(_report())
    # The overhead (static) row has no BEFORE, so its delta columns render n/a.
    overhead_line = next(line for line in text.splitlines() if "SKILL.md" in line)
    assert "n/a" in overhead_line


# ── render_csv ───────────────────────────────────────────────────────────────


def test_render_csv_header_and_rows():
    text = render_csv(_report())
    lines = text.strip().splitlines()
    assert lines[0] == "row,kind,mode,source,suspect,before,after,delta_abs,delta_pct"
    assert any("input_tokens" in line for line in lines[1:])


# ── render_jsonl (per-record evidence) ───────────────────────────────────────


def test_render_jsonl_emits_tokscale_fields():
    text = render_jsonl([_rec()])
    obj = json.loads(text.splitlines()[0])
    assert set(obj) == {
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "model",
        "timestamp",
        "session_id",
    }
    assert obj["input_tokens"] == 1000
    assert obj["model"] == MODEL


def test_render_jsonl_one_line_per_record():
    text = render_jsonl([_rec(), _rec(session_id="sess-2")])
    assert len(text.splitlines()) == 2


# ── to_daily_rollup ──────────────────────────────────────────────────────────


def test_to_daily_rollup_keeps_four_categories_split():
    rollup = to_daily_rollup([_rec(), _rec()])
    assert len(rollup) == 1
    entry = rollup[0]
    assert entry["input_tokens"] == 2000
    assert entry["output_tokens"] == 1000
    assert entry["cache_creation_input_tokens"] == 200
    assert entry["cache_read_input_tokens"] == 400
    assert "model" in entry and "day" in entry


def test_to_daily_rollup_unknown_timestamp():
    rollup = to_daily_rollup([_rec(timestamp=None)])
    assert rollup[0]["day"] == "unknown"


# ── aggregate ────────────────────────────────────────────────────────────────


def test_aggregate_by_phase():
    records = [_rec(phase="implement"), _rec(phase="review")]
    out = aggregate(records, ["phase"])
    phases = {row["phase"] for row in out}
    assert phases == {"implement", "review"}
    for row in out:
        assert row["input_tokens"] == 1000


def test_aggregate_by_role_uses_agent_label():
    out = aggregate([_rec(agent_label="sdet-1")], ["role"])
    assert out[0]["role"] == "sdet-1"


def test_aggregate_multi_key():
    out = aggregate([_rec(), _rec()], ["run", "model"])
    assert len(out) == 1
    assert out[0]["input_tokens"] == 2000
    assert out[0]["run"] == "run-1"
    assert out[0]["model"] == MODEL


def test_aggregate_rejects_unknown_group_by():
    with pytest.raises(ValueError):
        aggregate([_rec()], ["nonsense"])


def test_aggregate_saturating_sum():
    huge = {
        "usage": {
            "input_tokens": 2**62,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
        "model": MODEL,
        "run": "r",
    }
    out = aggregate([huge, dict(huge)], ["run"])
    assert out[0]["input_tokens"] == 2**63 - 1  # saturated, never overflows past ceiling
