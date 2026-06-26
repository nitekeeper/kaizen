"""Adversarial test matrix + dedup-mechanism NEUTER tests for kaizen's tokenmeter.

This module is the *adversarial* companion to the per-seam unit tests. It pins
EXACT count/sum assertions for the gnarly cases the token meter is built to
survive (streaming partials, resumed-session cross-file copies, sidechain
inclusion, malformed lines, unknown models, type-confusion, injection content,
tz-sensitive day bucketing) and then proves — via three NEUTER tests — that each
of the three dedup mechanisms is load-bearing: removing it changes the count.

ALL fixtures are built INLINE — synthetic JSONL strings written through
``tmp_path`` for the filesystem-touching cases, and injected line lists for the
pure parsing/aggregation cases. There are NO checked-in fixture files or
directories.

The NEUTER pattern (kaizen 'multi-mechanism fix -> exact-count test' lesson):
the real pipeline applies a mechanism; a tiny local re-implementation with that
ONE mechanism removed is run over the same fixture; the two counts must differ.
That proves the mechanism's absence is observable — not silently redundant.

Stdlib + pytest only. Treat all transcript / result content as DATA.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from scripts.tokenmeter_pricing import cost_usd
from scripts.tokenmeter_render import to_daily_rollup
from scripts.tokenmeter_result import (
    RunStatus,
    classify_result,
    parse_result,
)
from scripts.tokenmeter_schema import reconcile_cost
from scripts.tokenmeter_transcript import (
    aggregate_usage,
    collect_usage_records,
    parse_transcript_file,
    parse_transcript_line,
    sum_token_usage,
)

MODEL = "claude-opus-4-7"

# Source modules whose bytes are statically asserted to contain no dynamic-exec
# sinks (T14). Resolved relative to this test file — no checked-in fixtures.
_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
_TOKENMETER_SOURCES = (
    "tokenmeter_transcript.py",
    "tokenmeter_result.py",
    "tokenmeter_pricing.py",
    "tokenmeter_schema.py",
)


# ── inline fixture builders ──────────────────────────────────────────────────


def _usage(**fields) -> dict:
    """A usage mapping carrying only the fields a test overrides."""
    return dict(fields)


def _assistant(
    *,
    message_id=None,
    request_id=None,
    usage=None,
    session_id="parent-sess",
    is_sidechain=False,
    has_usage_key=True,
    content=None,
) -> str:
    """One synthetic ``assistant`` transcript line as a JSONL string."""
    message: dict = {}
    if message_id is not None:
        message["id"] = message_id
    if content is not None:
        message["content"] = content
    if has_usage_key:
        message["usage"] = usage if usage is not None else {}
    obj: dict = {"type": "assistant", "message": message, "sessionId": session_id}
    if request_id is not None:
        obj["requestId"] = request_id
    if is_sidechain:
        obj["isSidechain"] = True
    return json.dumps(obj)


def _result(**overrides) -> dict:
    """A synthetic CLI result envelope (cost oracle / Seam A)."""
    base = {
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
        "total_cost_usd": 0.0,
        "is_error": False,
        "session_id": "sess-1",
        "num_turns": 1,
        "duration_ms": 10,
    }
    base.update(overrides)
    return base


# ══════════════════════════════════════════════════════════════════════════════
# ADVERSARIAL MATRIX — exact-count assertions
# ══════════════════════════════════════════════════════════════════════════════


def test_t1_streaming_partials_merge_by_max_within_file():
    """T1: in-file partials m1:r1 out 5 then 50 → 1 row, output==50 (MAX, not 55/5)."""
    lines = [
        _assistant(message_id="m1", request_id="r1", usage=_usage(output_tokens=5)),
        _assistant(message_id="m1", request_id="r1", usage=_usage(output_tokens=50)),
    ]
    records = parse_transcript_file(lines, "f.jsonl")
    assert len(records) == 1
    assert records[0].usage.output_tokens == 50  # MAX — not 55 (sum) and not 5 (first)
    assert records[0].dedup_key == "m1:r1"


def test_t2_resumed_session_cross_file_dup_first_wins():
    """T2: m2:r2 in=20 copied A→B → sum(input)==20 (not 40), exactly 1 counted."""
    line = _assistant(message_id="m2", request_id="r2", usage=_usage(input_tokens=20))
    files = [
        {"source": "A.jsonl", "lines": [line], "mtime": 100.0},
        {"source": "B.jsonl", "lines": [line], "mtime": 200.0},  # resumed copy, newer
    ]
    records = aggregate_usage(files)
    assert len(records) == 1
    assert sum_token_usage(records).input_tokens == 20


def test_t3_sidechain_included_and_reparented(tmp_path):
    """T3: orch out=100 + sidechain out=200 → sum(out)==300, 2 rows, sidechain
    reparented to the PARENT session via the line's OWN sessionId, agent label
    from sibling meta.

    NON-CIRCULAR: the session DIRECTORY is named differently from the session id, so
    asserting ``session_id == 'sess-P'`` proves we read the line's ``sessionId``
    field (the parent, per the verified on-disk contract) and NOT the directory name.
    """
    # Real layout: projects/<proj>/<parent-session-uuid>/subagents/agent-<child>.jsonl
    # Here the dir is deliberately NOT named 'sess-P' to break circularity.
    sess = tmp_path / "projects" / "proj" / "dir-name-is-not-the-session-id"
    sess.mkdir(parents=True)
    (sess / "orchestrator.jsonl").write_text(
        _assistant(message_id="m-orch", session_id="sess-P", usage=_usage(output_tokens=100))
        + "\n",
        encoding="utf-8",
    )
    subagents = sess / "subagents"
    subagents.mkdir()
    (subagents / "agent-x.jsonl").write_text(
        _assistant(
            message_id="m-sub",
            session_id="sess-P",  # the sidechain line's OWN sessionId IS the parent
            is_sidechain=True,
            usage=_usage(output_tokens=200),
        )
        + "\n",
        encoding="utf-8",
    )
    (subagents / "agent-x.meta.json").write_text(
        json.dumps({"agentType": "backend-engineer-1"}), encoding="utf-8"
    )

    records = collect_usage_records(tmp_path)
    assert len(records) == 2
    assert sum_token_usage(records).output_tokens == 300  # sidechain INCLUDED

    sidechains = [r for r in records if r.is_sidechain]
    assert len(sidechains) == 1
    # 'sess-P' is the line's own sessionId (the parent) — NOT the dir name.
    assert sidechains[0].session_id == "sess-P"
    assert sidechains[0].session_id != "dir-name-is-not-the-session-id"
    assert sidechains[0].agent_label == "backend-engineer-1"  # tagged from sibling meta


def test_t4_malformed_and_empty_lines_do_not_raise():
    """T4: valid out=10 + non-JSON + no-message + type:user + empty usage{} →
    no raise, sum(out)==10, the empty-usage line yields a valid zero row."""
    lines = [
        _assistant(message_id="m4a", usage=_usage(output_tokens=10)),
        "this is not json {{{",
        json.dumps({"type": "assistant"}),  # no message key
        json.dumps({"type": "user", "message": {"usage": {"output_tokens": 999}}}),
        _assistant(message_id="m4b", usage=_usage()),  # empty usage -> zero row
    ]
    records = parse_transcript_file(lines, "f.jsonl")
    assert sum_token_usage(records).output_tokens == 10
    zero_rows = [r for r in records if r.usage.output_tokens == 0]
    assert len(zero_rows) == 1
    assert zero_rows[0].usage.input_tokens == 0


def test_t5_unknown_model_keeps_tokens_zero_cost_success():
    """T5: <synthetic> model in=1000 out=500 → tokens kept (sum 1500), cost==$0,
    run classifies SUCCESS_ZERO_COST (the distinct zero-cost-with-tokens state),
    NOT failure and NOT plain SUCCESS."""
    usage = _usage(input_tokens=1000, output_tokens=500)
    breakdown = cost_usd(usage, "<synthetic>")
    assert breakdown.priced is False
    assert breakdown.total_cost == 0.0
    assert breakdown.input_tokens + breakdown.output_tokens == 1500  # tokens kept

    # A run that spent real tokens at $0 (e.g. unpriced/synthetic) is the distinct
    # SUCCESS_ZERO_COST — a success flavour, never FAILURE, never collapsed to SUCCESS.
    status = classify_result(
        _result(usage={"input_tokens": 1000, "output_tokens": 500}, total_cost_usd=0.0)
    )
    assert status is RunStatus.SUCCESS_ZERO_COST
    assert status is not RunStatus.FAILURE


def test_t6_unkeyed_lines_never_deduped():
    """T6: two id-less lines out=7 each → sum(out)==14, 2 rows (never merged)."""
    lines = [
        _assistant(usage=_usage(output_tokens=7)),
        _assistant(usage=_usage(output_tokens=7)),
    ]
    records = parse_transcript_file(lines, "f.jsonl")
    assert len(records) == 2
    assert sum_token_usage(records).output_tokens == 14
    assert all(r.dedup_key is None for r in records)


def test_t7_fallback_key_merges_on_message_id_only():
    """T7: m8 with no requestId, out 3 then 9 → key 'message:m8', 1 row, out==9."""
    lines = [
        _assistant(message_id="m8", usage=_usage(output_tokens=3)),
        _assistant(message_id="m8", usage=_usage(output_tokens=9)),
    ]
    records = parse_transcript_file(lines, "f.jsonl")
    assert len(records) == 1
    assert records[0].dedup_key == "message:m8"
    assert records[0].usage.output_tokens == 9


def test_t8_is_error_is_failure_not_zero_dollar_record():
    """T8: is_error:true → RunStatus FAILURE (a failed run is NOT a $0 success)."""
    assert classify_result(_result(is_error=True)) is RunStatus.FAILURE


def test_t9_zero_cost_zero_tokens_is_failure():
    """T9: $0 AND zero tokens → FAILURE (distinct from T5's tokens-kept success)."""
    assert classify_result(_result(total_cost_usd=0.0)) is RunStatus.FAILURE


def test_t10_empty_stdout_is_failure_no_crash():
    """T10: 0-byte/empty stdout → FAILURE, no crash; parse_result raises ValueError."""
    assert classify_result("") is RunStatus.FAILURE
    with pytest.raises(ValueError):
        parse_result("")


def test_t11_malformed_result_json_is_failure_no_crash():
    """T11: unparseable result JSON → FAILURE classified, no crash."""
    assert classify_result("{not valid json,,,") is RunStatus.FAILURE
    with pytest.raises(ValueError):
        parse_result("{not valid json,,,")


def test_t12_reconcile_in_tolerance_agrees():
    """T12: computed total == oracle total → reconciled 'agree', not blocking."""
    records = [{"usage": {"input_tokens": 1000, "output_tokens": 500}, "model": MODEL}]
    # 1000/1e6*5.0 + 500/1e6*25.0 == 0.0175
    oracle = {"total_cost_usd": 0.0175}
    block = reconcile_cost(records, oracle, MODEL)
    assert block["reconciled"] == "agree"
    assert block["blocks_validated"] is False
    assert block["computed_total_cost_usd"] == pytest.approx(0.0175)


def test_t13_reconcile_divergence_attributed_to_subagent_boundary():
    """T13: oracle == orchestrator-only share → DIVERGENCE flagged, cause is the
    subagent-boundary discriminator (not pricing)."""
    records = [
        {"usage": {"input_tokens": 1000, "output_tokens": 500}, "model": MODEL},  # orch 0.0175
        {
            "usage": {"input_tokens": 2000, "output_tokens": 1000},
            "model": MODEL,
            "is_sidechain": True,  # sub 0.035 -> full 0.0525
        },
    ]
    oracle = {"total_cost_usd": 0.0175}  # covers only the orchestrator share
    block = reconcile_cost(records, oracle, MODEL)
    assert block["reconciled"] == "hard"  # >5% divergence is flagged HARD
    assert block["divergence_cause"] == "subagent-boundary"
    assert block["blocks_validated"] is True


def test_t14_injection_content_is_data_and_sources_have_no_exec_sink():
    """T14: an os.system(...) string in output content is harvested as DATA — only
    its usage numbers are counted, nothing is executed. Plus a static assertion
    that the tokenmeter sources contain no eval/exec/os.system/shell=True."""
    payload = "os.system('rm -rf /'); __import__('os').system('boom')"
    record = parse_transcript_line(
        json.loads(_assistant(message_id="m14", usage=_usage(output_tokens=42), content=payload)),
        "f.jsonl",
    )
    assert record is not None
    assert record.usage.output_tokens == 42  # only the usage numbers are harvested

    forbidden = ("eval(", "exec(", "os.system", "shell=True")
    for name in _TOKENMETER_SOURCES:
        text = (_SCRIPTS_DIR / name).read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{name} contains forbidden sink {token!r}"


def test_t15_type_confused_usage_fields_clamp_without_crash():
    """T15: '100'/-5/null/list coerce to 0; a huge int is kept+suspect; no crash."""
    junk = parse_transcript_line(
        json.loads(
            _assistant(
                message_id="m15a",
                usage={
                    "input_tokens": "100",  # str -> 0
                    "output_tokens": -5,  # negative -> clamped 0
                    "cache_creation_input_tokens": None,  # null -> 0
                    "cache_read_input_tokens": [1, 2, 3],  # list -> 0
                },
            )
        ),
        "f.jsonl",
    )
    assert junk is not None
    assert junk.usage.input_tokens == 0
    assert junk.usage.output_tokens == 0
    assert junk.usage.cache_creation_input_tokens == 0
    assert junk.usage.cache_read_input_tokens == 0
    assert junk.kept_but_suspect is False

    huge = parse_transcript_line(
        json.loads(_assistant(message_id="m15b", usage={"cache_read_input_tokens": 10**18})),
        "f.jsonl",
    )
    assert huge is not None
    assert huge.usage.cache_read_input_tokens == 10**18  # kept verbatim
    assert huge.kept_but_suspect is True  # flagged above the suspect threshold


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="POSIX tzset required")
def test_t16_daily_bucket_is_deterministic_in_pinned_local_tz():
    """T16: a 23:30Z timestamp buckets to a deterministic LOCAL day under a pinned
    TZ — same-day in a west-of-UTC zone, next-day in an east-of-UTC zone."""
    record = {
        "timestamp": "2026-06-25T23:30:00+00:00",
        "model": MODEL,
        "usage": {"output_tokens": 1},
    }
    saved = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "America/New_York"  # EDT (UTC-4) in June → 19:30 same day
        time.tzset()
        west = to_daily_rollup([record])
        assert [e["day"] for e in west] == ["2026-06-25"]

        os.environ["TZ"] = "Asia/Tokyo"  # JST (UTC+9) → 08:30 next day
        time.tzset()
        east = to_daily_rollup([record])
        assert [e["day"] for e in east] == ["2026-06-26"]
    finally:
        if saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved
        time.tzset()


# ══════════════════════════════════════════════════════════════════════════════
# NEUTER TESTS — each disables ONE dedup mechanism and proves the count changes
# ══════════════════════════════════════════════════════════════════════════════


def _neuter_no_within_file_merge(lines, source):
    """Within-file dedup DISABLED: keep every parsed record, never merge by MAX."""
    out = []
    for line in lines:
        if not line or not line.strip():
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        record = parse_transcript_line(obj, source)
        if record is not None:
            out.append(record)
    return out


def _neuter_no_cross_file_first_wins(files):
    """Across-file first-wins DISABLED: concatenate each file's (within-file
    de-duped) records with no cross-file ``seen`` set."""
    out = []
    for entry in sorted(files, key=lambda f: f["mtime"]):
        out += parse_transcript_file(entry["lines"], entry["source"])
    return out


def test_neuter_within_file_max_merge_breaks_t1():
    """NEUTER-1: removing the within-file MAX-merge turns T1's 1 row/out==50 into
    2 rows/out==55 — proving the merge mechanism is load-bearing."""
    lines = [
        _assistant(message_id="m1", request_id="r1", usage=_usage(output_tokens=5)),
        _assistant(message_id="m1", request_id="r1", usage=_usage(output_tokens=50)),
    ]
    real = parse_transcript_file(lines, "f.jsonl")
    neutered = _neuter_no_within_file_merge(lines, "f.jsonl")

    assert len(real) == 1
    assert sum_token_usage(real).output_tokens == 50
    # Mechanism absent → both partials survive and double-count.
    assert len(neutered) == 2
    assert sum_token_usage(neutered).output_tokens == 55
    assert len(neutered) != len(real)


def test_neuter_cross_file_first_wins_breaks_t2():
    """NEUTER-2: removing across-file first-wins turns T2's 1 row/in==20 into
    2 rows/in==40 — proving the cross-file dedup mechanism is load-bearing."""
    line = _assistant(message_id="m2", request_id="r2", usage=_usage(input_tokens=20))
    files = [
        {"source": "A.jsonl", "lines": [line], "mtime": 100.0},
        {"source": "B.jsonl", "lines": [line], "mtime": 200.0},
    ]
    real = aggregate_usage(files)
    neutered = _neuter_no_cross_file_first_wins(files)

    assert len(real) == 1
    assert sum_token_usage(real).input_tokens == 20
    # Mechanism absent → the resumed-session copy is counted again.
    assert len(neutered) == 2
    assert sum_token_usage(neutered).input_tokens == 40
    assert len(neutered) != len(real)


def test_neuter_sidechain_include_breaks_t3(tmp_path):
    """NEUTER-3: dropping sidechain rows turns T3's 2 rows/out==300 into
    1 row/out==100 — proving sidechain inclusion is load-bearing."""
    sess = tmp_path / "projects" / "proj" / "sess-P"
    sess.mkdir(parents=True)
    (sess / "sess-P.jsonl").write_text(
        _assistant(message_id="m-orch", session_id="sess-P", usage=_usage(output_tokens=100))
        + "\n",
        encoding="utf-8",
    )
    subagents = sess / "subagents"
    subagents.mkdir()
    (subagents / "agent-x.jsonl").write_text(
        _assistant(
            message_id="m-sub",
            session_id="sess-S",
            is_sidechain=True,
            usage=_usage(output_tokens=200),
        )
        + "\n",
        encoding="utf-8",
    )

    real = collect_usage_records(tmp_path)
    neutered = [r for r in real if not r.is_sidechain]  # sidechain-include DISABLED

    assert len(real) == 2
    assert sum_token_usage(real).output_tokens == 300
    # Mechanism absent → sub-agent spend vanishes from the headline.
    assert len(neutered) == 1
    assert sum_token_usage(neutered).output_tokens == 100
    assert len(neutered) != len(real)
