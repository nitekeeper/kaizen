"""End-to-end tokenmeter tests on synthetic transcript TREES (``tmp_path``).

These drive the REAL pipeline — ``collect_usage_records`` (Seam B) → ``cost_usd``
(pricing) → ``assemble`` (schema) → ``render_json`` / ``render_jsonl`` /
``to_daily_rollup`` (render) — over synthetic on-disk JSONL, so a feature that is
wired but DEAD on the real frozen ``UsageRecord`` cannot pass here (the failure mode
the dead-path fixes target: dict fixtures carried ``timestamp`` / ``model`` /
``run`` / ``phase`` / ``cache_creation`` keys the real record lacked).

The synthetic lines mirror the shape CONFIRMED against real ``~/.claude``
transcripts (verified 2026-06-25): an ``assistant`` line carries a top-level
``sessionId`` + ``timestamp``, ``message.model``, and
``message.usage.cache_creation.{ephemeral_5m_input_tokens,
ephemeral_1h_input_tokens}``. Sidechain lines additionally carry
``isSidechain: true`` and live under
``projects/<proj>/<parent-session-uuid>/subagents/agent-<child>.jsonl`` — and their
OWN ``sessionId`` field is the parent session.

Stdlib + pytest only. Transcript content is DATA.
"""

from __future__ import annotations

import dataclasses
import json
import os
import time

import pytest

from scripts.tokenmeter_pricing import CACHE_WRITE_1H, cost_usd
from scripts.tokenmeter_render import render_json, render_jsonl, to_daily_rollup
from scripts.tokenmeter_schema import _record_cost, assemble, validate_report
from scripts.tokenmeter_transcript import collect_usage_records, parse_transcript_line

MODEL = "claude-haiku-4-5"  # base input rate 1.0 → easy exact TTL arithmetic


def _assistant_line(
    *,
    message_id,
    usage,
    session_id,
    timestamp,
    model=MODEL,
    is_sidechain=False,
):
    """One synthetic ``assistant`` transcript line matching the real on-disk shape."""
    obj = {
        "type": "assistant",
        "sessionId": session_id,
        "timestamp": timestamp,
        "message": {"id": message_id, "model": model, "usage": usage},
    }
    if is_sidechain:
        obj["isSidechain"] = True
    return json.dumps(obj)


def _meta(**overrides):
    base = {
        "target": "github.com/x/y",
        "target_commit": "abc123",
        "transport": "cli",
        "model": MODEL,
        "effort": "high",
        "caching": True,
        "cycles": 1,
        "subject": "tokenmeter",
        "scenario_source": "scenario-v1",
        "claude_config_dir": "/home/u/.claude",
    }
    base.update(overrides)
    return base


# ── Finding A: cache-write TTL split is captured + priced EXACT (1h at 2.0x) ──


def test_finding_a_real_parsed_record_1h_cache_write_prices_exact_2x():
    """A REAL parsed record carrying ``ephemeral_1h_input_tokens`` prices through the
    EXACT TTL-split path (source='exact', 1h at 2.0x) — NOT the flat 5m
    approximation that under-counted a 1h workload by 37.5%."""
    line = _assistant_line(
        message_id="m1",
        session_id="sess",
        timestamp="2026-06-21T01:42:39.428Z",
        usage={
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 1_000_000,
            "cache_read_input_tokens": 0,
            # The nested TTL split Seam B previously DISCARDED.
            "cache_creation": {
                "ephemeral_5m_input_tokens": 0,
                "ephemeral_1h_input_tokens": 1_000_000,
            },
        },
    )
    rec = parse_transcript_line(json.loads(line), "/x/sess/subagents/agent-z.jsonl")

    # Seam B captured the nested split onto the REAL record (it is no longer dropped).
    assert rec.cache_creation_1h == 1_000_000
    assert rec.cache_creation_5m == 0
    assert rec.model == MODEL

    # Price it through the production wiring (record → cost_usd via the schema helper).
    breakdown = _record_cost(rec, MODEL)
    assert breakdown.source == "exact"  # the dead exact-split path now FIRES on a real record
    assert breakdown.cache_write_1h_tokens == 1_000_000
    assert breakdown.cache_write_5m_tokens == 0
    # haiku base input 1.0 → 1h at 2.0x.
    assert breakdown.cache_write_cost == pytest.approx(1.0 * CACHE_WRITE_1H)
    assert breakdown.cache_write_cost == pytest.approx(2.0)

    # Contrast: pricing the four-category usage ALONE (no split, the pre-fix path)
    # falls back to the flat 5m approximation and under-counts the same tokens.
    flat = cost_usd(rec.usage, MODEL)
    assert flat.source == "approximated"
    assert flat.cache_write_cost == pytest.approx(1.25)
    assert breakdown.cache_write_cost > flat.cache_write_cost  # the gap the fix recovers


# ── Finding B: RFC3339 timestamp → LOCAL daily bucket + non-null jsonl ────────


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="POSIX tzset required")
def test_finding_b_real_record_timestamp_local_bucket_and_jsonl(tmp_path):
    """A REAL collected record's RFC3339 ``timestamp`` is parsed to epoch ms and the
    daily rollup buckets it in the LOCAL tz (not ``day='unknown'``); ``render_jsonl``
    emits a NON-NULL tokscale timestamp (it was ``null`` when the field was dead)."""
    proj = tmp_path / "projects" / "proj" / "session-uuid"
    proj.mkdir(parents=True)
    (proj / "orchestrator.jsonl").write_text(
        _assistant_line(
            message_id="m1",
            session_id="session-uuid",
            timestamp="2026-06-25T23:30:00+00:00",
            usage={
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        )
        + "\n",
        encoding="utf-8",
    )

    records = collect_usage_records(config_dir=tmp_path)
    assert len(records) == 1
    rec = records[0]
    # Seam B parsed the RFC3339 timestamp into epoch ms on the REAL record.
    assert rec.timestamp == "2026-06-25T23:30:00+00:00"
    assert rec.ts_epoch_ms is not None

    # render_jsonl emits a NON-NULL tokscale timestamp + model.
    evidence = json.loads(render_jsonl(records).splitlines()[0])
    assert evidence["timestamp"] == "2026-06-25T23:30:00+00:00"
    assert evidence["model"] == MODEL

    # LOCAL-tz daily bucket: a 23:30Z stamp buckets same-day west of UTC, next-day east.
    saved = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "America/New_York"  # EDT (UTC-4) → 19:30 same day
        time.tzset()
        assert [e["day"] for e in to_daily_rollup(records)] == ["2026-06-25"]

        os.environ["TZ"] = "Asia/Tokyo"  # JST (UTC+9) → 08:30 next day
        time.tzset()
        assert [e["day"] for e in to_daily_rollup(records)] == ["2026-06-26"]
    finally:
        if saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved
        time.tzset()


# ── Full chain: collect → assemble → render, plus run/phase tagging goes live ─


def test_e2e_collect_assemble_render_chain(tmp_path):
    """The whole pipeline on a synthetic tree: TTL pricing, timestamps, sidechain
    reparenting, and (when tagged) run/phase aggregation are ALL live end-to-end."""
    proj = tmp_path / "projects" / "proj" / "parent-session-uuid"
    proj.mkdir(parents=True)
    (proj / "orchestrator.jsonl").write_text(
        _assistant_line(
            message_id="m-orch",
            session_id="parent-session-uuid",
            timestamp="2026-06-25T12:00:00+00:00",
            usage={
                "input_tokens": 1000,
                "output_tokens": 200,
                "cache_creation_input_tokens": 500_000,
                "cache_read_input_tokens": 3000,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 0,
                    "ephemeral_1h_input_tokens": 500_000,
                },
            },
        )
        + "\n",
        encoding="utf-8",
    )
    subagents = proj / "subagents"
    subagents.mkdir()
    (subagents / "agent-a.jsonl").write_text(
        _assistant_line(
            message_id="m-sub",
            session_id="parent-session-uuid",  # the line's own sessionId IS the parent
            timestamp="2026-06-25T12:05:00+00:00",
            is_sidechain=True,
            usage={
                "input_tokens": 2000,
                "output_tokens": 50,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 100_000,
            },
        )
        + "\n",
        encoding="utf-8",
    )
    (subagents / "agent-a.meta.json").write_text(
        json.dumps({"agentType": "backend-engineer-1"}), encoding="utf-8"
    )

    records = collect_usage_records(config_dir=tmp_path)
    assert len(records) == 2

    # Sidechain INCLUDED + reparented to the parent via its OWN sessionId, labelled.
    sidechain = next(r for r in records if r.is_sidechain)
    assert sidechain.session_id == "parent-session-uuid"
    assert sidechain.agent_label == "backend-engineer-1"

    # assemble → a structurally valid report.
    outcomes = {
        "cycles_succeeded": 1,
        "cycles_abandoned": 0,
        "pr_opened": True,
        "tests_green": True,
    }
    report = assemble(records, [], outcomes=outcomes, metadata=_meta())
    assert validate_report(report) is True

    # The orchestrator's 1h cache write prices EXACT (not approximated) end-to-end.
    orch = next(r for r in records if not r.is_sidechain)
    orch_cost = _record_cost(orch, MODEL)
    assert orch_cost.source == "exact"
    assert orch_cost.cache_write_1h_tokens == 500_000

    # render_json round-trips; jsonl carries non-null timestamps + model for BOTH records.
    parsed = json.loads(render_json(report))
    assert parsed["schema_version"]
    jsonl_objs = [json.loads(x) for x in render_jsonl(records).splitlines()]
    assert len(jsonl_objs) == 2
    assert all(o["timestamp"] is not None for o in jsonl_objs)
    assert all(o["model"] == MODEL for o in jsonl_objs)

    # Daily rollup buckets to a REAL day (not 'unknown'), four categories kept split.
    rollup = to_daily_rollup(records)
    assert rollup
    assert all(e["day"] != "unknown" for e in rollup)

    # run/phase tagging (the Cycle-2 wiring point) → per-phase rows + multi-run CV.
    tagged = [
        dataclasses.replace(records[0], run="run-1", phase="implement"),
        dataclasses.replace(records[1], run="run-2", phase="review"),
        dataclasses.replace(records[0], run="run-3", phase="implement"),
    ]
    tagged_report = assemble(tagged, [], outcomes=outcomes, metadata=_meta())
    phase_rows = {r["row"] for r in tagged_report["rows"] if r["kind"] == "phase"}
    assert {"implement", "review"} <= phase_rows
    input_row = next(r for r in tagged_report["rows"] if r["row"] == "input_tokens")
    assert input_row["after"]["n"] >= 2  # >1 run → a real CV is computed
    assert input_row["after"]["cv"] is not None
    assert tagged_report["metadata"]["n_runs"] == 3
