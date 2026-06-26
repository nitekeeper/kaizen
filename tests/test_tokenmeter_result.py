"""Tests for Seam A — result cost oracle (scripts.tokenmeter_result).

All fixtures are inline; the injectable runner is a tiny async fake matching the
``FakeCliRunner`` shape — no real ``claude`` is ever spawned.
"""

from __future__ import annotations

import asyncio

from scripts.tokenmeter_result import (
    RunStatus,
    classify_result,
    parse_result,
    run_and_classify,
)


def _result(**overrides) -> dict:
    base = {
        "usage": {
            "input_tokens": 3,
            "output_tokens": 5,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
        "total_cost_usd": 0.012,
        "is_error": False,
        "subtype": "success",
        "session_id": "sess-1",
        "num_turns": 2,
        "duration_ms": 1234,
        "stop_reason": "end_turn",
    }
    base.update(overrides)
    return base


# ── parse_result ─────────────────────────────────────────────────────────────


def test_parse_result_extracts_all_fields():
    obj = parse_result(_result(modelUsage={"claude-opus": {"input_tokens": 3}}))
    assert obj.total_cost_usd == 0.012
    assert obj.usage.input_tokens == 3
    assert obj.usage.output_tokens == 5
    assert obj.session_id == "sess-1"
    assert obj.num_turns == 2
    assert obj.duration_ms == 1234
    assert obj.is_error is False
    assert obj.stop_reason == "end_turn"
    assert obj.model_usage == {"claude-opus": {"input_tokens": 3}}


def test_parse_result_accepts_json_string():
    import json

    obj = parse_result(json.dumps(_result()))
    assert obj.usage.output_tokens == 5


def test_parse_result_missing_usage_is_zero():
    obj = parse_result({"total_cost_usd": 0.5})
    assert obj.usage.input_tokens == 0
    assert obj.usage.output_tokens == 0
    assert obj.total_cost_usd == 0.5


def test_parse_result_hardening():
    obj = parse_result(_result(usage={"input_tokens": True, "output_tokens": "9"}))
    assert obj.usage.input_tokens == 0  # bool rejected
    assert obj.usage.output_tokens == 0  # str rejected


def test_parse_result_empty_blob_raises():
    import pytest

    with pytest.raises(ValueError):
        parse_result("")


def test_parse_result_unparseable_raises():
    import pytest

    with pytest.raises(ValueError):
        parse_result("{not json")


# ── classify_result (fail-loud) ──────────────────────────────────────────────


def test_classify_success():
    assert classify_result(_result()) is RunStatus.SUCCESS


def test_classify_is_error_is_failure():
    assert classify_result(_result(is_error=True)) is RunStatus.FAILURE


def test_classify_zero_byte_is_failure():
    assert classify_result("") is RunStatus.FAILURE
    assert classify_result(b"") is RunStatus.FAILURE


def test_classify_unparseable_is_failure():
    assert classify_result("{broken") is RunStatus.FAILURE
    assert classify_result(None) is RunStatus.FAILURE


def test_classify_zero_cost_and_zero_tokens_is_failure():
    raw = _result(
        total_cost_usd=0.0,
        usage={
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    )
    # A failed run is NOT a $0 success.
    assert classify_result(raw) is RunStatus.FAILURE


def test_classify_zero_cost_with_tokens_is_success_zero_cost():
    # Real tokens but $0 cost (e.g. fully cached / unpriced model) is the distinct
    # SUCCESS_ZERO_COST — a flavour of success (NOT a failure), kept visible rather
    # than collapsed into plain SUCCESS.
    raw = _result(total_cost_usd=0.0)
    status = classify_result(raw)
    assert status is RunStatus.SUCCESS_ZERO_COST
    assert status is not RunStatus.FAILURE


def test_classify_blocked_outcome_with_tokens_is_success_run():
    # A terminal `blocked` task outcome that still spent tokens is a SUCCESS run
    # (task outcome != run success). At $0 it surfaces as SUCCESS_ZERO_COST — still
    # a success flavour, never FAILURE. Mirrors the host FakeCliRunner blocked shape.
    raw = {
        "usage": {"output_tokens": 5, "input_tokens": 3},
        "total_cost_usd": 0.0,
        "is_error": False,
        "structured_output": {"type": "task_result", "status": "blocked"},
    }
    status = classify_result(raw)
    assert status is RunStatus.SUCCESS_ZERO_COST
    assert status is not RunStatus.FAILURE


# ── injectable runner (no real claude) ───────────────────────────────────────


class _FakeRunner:
    """Async runner matching the FakeCliRunner shape (no real process)."""

    no_real_process = True
    is_fake = True

    def __init__(self, raw):
        self._raw = raw
        self.calls: list[tuple] = []

    async def __call__(self, argv, cwd):
        self.calls.append((tuple(argv), cwd))
        return self._raw


def test_run_and_classify_success():
    runner = _FakeRunner(_result())
    status, obj = asyncio.run(run_and_classify(runner, ["claude", "-p", "go"], "/tmp"))
    assert status is RunStatus.SUCCESS
    assert obj is not None
    assert obj.usage.input_tokens == 3
    assert runner.calls == [(("claude", "-p", "go"), "/tmp")]


def test_run_and_classify_failure_returns_none_object():
    runner = _FakeRunner("")  # 0-byte
    status, obj = asyncio.run(run_and_classify(runner, ["claude"], "/tmp"))
    assert status is RunStatus.FAILURE
    assert obj is None
