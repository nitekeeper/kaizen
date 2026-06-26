"""Tests for Seam assembly + reconciliation (scripts.tokenmeter_schema).

All fixtures are inline dicts (duck-typed like the real UsageRecord/ResultObject
shapes); no filesystem trees are created here — see test_seam_a_aggregation.py for
the transcript-tree reconciliation tests.
"""

from __future__ import annotations

import pytest

from scripts.tokenmeter_schema import (
    CATEGORY_FIELDS,
    ControlDriftError,
    ReportValidationError,
    assemble,
    assert_controls_match,
    controls_match,
    dynamic_figure,
    is_validated,
    reconcile_cost,
    validate_report,
)

MODEL = "claude-opus-4-7"


def _rec(**overrides):
    base = {
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 500,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 200,
        },
        "model": MODEL,
        "run": "run-1",
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
        "target_commit": "abc123",
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


def _outcomes(**overrides):
    base = {
        "cycles_succeeded": 2,
        "cycles_abandoned": 1,
        "pr_opened": True,
        "tests_green": True,
    }
    base.update(overrides)
    return base


# ── dynamic_figure ───────────────────────────────────────────────────────────


def test_dynamic_figure_single_run_is_directional():
    fig = dynamic_figure([1000])
    assert fig == {"n": 1, "mean": 1000.0, "cv": None, "confidence": "directional"}


def test_dynamic_figure_multi_run_has_cv():
    fig = dynamic_figure([100, 100, 100])
    assert fig["n"] == 3
    assert fig["mean"] == 100.0
    assert fig["cv"] == 0.0
    assert fig["confidence"] == "stable"


def test_dynamic_figure_noisy():
    fig = dynamic_figure([10, 100])
    assert fig["confidence"] == "noisy"
    assert fig["cv"] is not None


def test_dynamic_figure_empty():
    assert dynamic_figure([])["confidence"] == "none"


# ── assemble: shape + invariants ─────────────────────────────────────────────


def test_assemble_produces_valid_report():
    report = assemble([_rec()], [], outcomes=_outcomes(), oracle=None, metadata=_meta())
    assert report["schema_version"]
    assert validate_report(report) is True
    assert report["metadata"]["scenario_hash"]  # derived from scenario_source
    assert report["metadata"]["rate_table_as_of"]


def test_assemble_keeps_four_categories_split():
    report = assemble([_rec()], [], outcomes=_outcomes(), metadata=_meta())
    category_rows = [r for r in report["rows"] if r["kind"] == "category"]
    names = {r["row"] for r in category_rows}
    # All four categories present as DISTINCT rows; never a single summed total.
    assert names == set(CATEGORY_FIELDS)
    assert len(category_rows) == 4


def test_assemble_category_rows_are_dynamic_measured():
    report = assemble([_rec()], [], outcomes=_outcomes(), metadata=_meta())
    for row in report["rows"]:
        if row["kind"] == "category":
            assert row["mode"] == "dynamic"
            assert row["source"] == "measured"
            assert row["after"]["confidence"] == "directional"


def test_assemble_overhead_rows_static():
    static_rows = [{"path": "skills/x/SKILL.md", "token_count": 320, "source": "approximated"}]
    report = assemble([_rec()], static_rows, outcomes=_outcomes(), metadata=_meta())
    overhead = [r for r in report["rows"] if r["kind"] == "overhead"]
    assert len(overhead) == 1
    assert overhead[0]["mode"] == "static"
    assert overhead[0]["after"] == 320


def test_assemble_derived_rows_present():
    report = assemble([_rec()], [], outcomes=_outcomes(), metadata=_meta())
    names = {d["row"] for d in report["derived"]}
    assert names == {
        "cache_hit_rate",
        "tokens_per_call",
        "cost_usd",
        "effective_unit_cost_per_accepted_cycle",
    }
    cost_row = next(d for d in report["derived"] if d["row"] == "cost_usd")
    assert cost_row["source"] == "oracle"


def test_assemble_outcome_block():
    report = assemble([_rec()], [], outcomes=_outcomes(), metadata=_meta())
    assert report["outcome"] == {
        "cycles_succeeded": 2,
        "cycles_abandoned": 1,
        "pr_opened": True,
        "tests_green": True,
    }


def test_assemble_suspect_propagates():
    report = assemble([_rec(kept_but_suspect=True)], [], outcomes=_outcomes(), metadata=_meta())
    assert any(r["suspect"] for r in report["rows"] if r["kind"] == "category")


# ── validator: raises on null source / mode / missing metadata ───────────────


def test_validate_raises_on_null_source():
    report = assemble([_rec()], [], outcomes=_outcomes(), metadata=_meta())
    report["rows"][0]["source"] = None
    with pytest.raises(ReportValidationError):
        validate_report(report)


def test_validate_raises_on_null_mode():
    report = assemble([_rec()], [], outcomes=_outcomes(), metadata=_meta())
    report["rows"][0]["mode"] = None
    with pytest.raises(ReportValidationError):
        validate_report(report)


def test_validate_raises_on_missing_metadata_key():
    report = assemble([_rec()], [], outcomes=_outcomes(), metadata=_meta())
    del report["metadata"]["model"]
    with pytest.raises(ReportValidationError):
        validate_report(report)


def test_validate_raises_on_null_derived_source():
    report = assemble([_rec()], [], outcomes=_outcomes(), metadata=_meta())
    report["derived"][0]["source"] = None
    with pytest.raises(ReportValidationError):
        validate_report(report)


# ── control-vector equality gate ─────────────────────────────────────────────


def test_controls_match_helpers():
    assert controls_match(_meta(), _meta()) is True
    assert controls_match(_meta(model="claude-opus-4-7"), _meta(model="fable-5")) is False


def test_assert_controls_match_raises_on_drift():
    with pytest.raises(ControlDriftError):
        assert_controls_match(_meta(model=MODEL), _meta(model="fable-5"))


def test_assemble_with_before_refuses_on_control_drift():
    before = assemble([_rec()], [], outcomes=_outcomes(), metadata=_meta())
    # effort drifted high -> low: the before/after delta must be refused.
    with pytest.raises(ControlDriftError):
        assemble(
            [_rec()],
            [],
            outcomes=_outcomes(),
            metadata=_meta(effort="low"),
            before=before,
        )


def test_assemble_with_before_computes_deltas_when_controls_match():
    before = assemble([_rec()], [], outcomes=_outcomes(), metadata=_meta())
    bigger_usage = {
        "input_tokens": 2000,
        "output_tokens": 500,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 200,
    }
    after = assemble(
        [_rec(usage=bigger_usage)],
        [],
        outcomes=_outcomes(),
        metadata=_meta(),
        before=before,
    )
    input_row = next(r for r in after["rows"] if r["row"] == "input_tokens")
    assert input_row["before"]["mean"] == 1000.0
    assert input_row["after"]["mean"] == 2000.0
    assert input_row["delta_abs"] == 1000.0
    assert input_row["delta_pct"] == 100.0


# ── reconciliation (no oracle) ───────────────────────────────────────────────


def test_reconcile_no_oracle_is_unreconciled():
    block = reconcile_cost([_rec()], None, MODEL)
    assert block["reconciled"] == "unreconciled"
    assert block["seam_a_total_cost_usd"] is None
    assert block["computed_total_cost_usd"] > 0


def test_reconcile_agree_within_tolerance():
    records = [_rec()]
    computed = reconcile_cost(records, None, MODEL)["computed_total_cost_usd"]
    block = reconcile_cost(records, {"total_cost_usd": computed}, MODEL)
    assert block["reconciled"] == "agree"
    assert is_validated({"cost_oracle": block}) is True


def test_reconcile_hard_blocks_validated():
    records = [_rec()]
    computed = reconcile_cost(records, None, MODEL)["computed_total_cost_usd"]
    block = reconcile_cost(records, {"total_cost_usd": computed * 3}, MODEL)
    assert block["reconciled"] == "hard"
    assert block["blocks_validated"] is True
    assert is_validated({"cost_oracle": block}) is False
