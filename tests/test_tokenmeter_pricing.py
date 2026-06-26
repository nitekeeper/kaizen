"""Tests for scripts/tokenmeter_pricing.py — four-category, cache-aware pricing.

Usage records are duck-typed (the production type is
``scripts.tokenmeter_model.TokenUsage``); these tests use lightweight
``SimpleNamespace`` stubs so the pricing logic can be exercised in isolation.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from scripts.tokenmeter_pricing import (
    CACHE_READ,
    CACHE_WRITE_1H,
    CACHE_WRITE_5M,
    PRICING,
    PRICING_AS_OF,
    CostBreakdown,
    canonicalize,
    cost_usd,
)

_MTOK = 1_000_000.0


def _usage(**kwargs):
    """Build a duck-typed usage record. cache_creation may be passed as a dict."""
    cc = kwargs.pop("cache_creation", None)
    if isinstance(cc, dict):
        cc = SimpleNamespace(**cc)
    return SimpleNamespace(cache_creation=cc, **kwargs)


# ── Unknown / synthetic models: $0 but keep tokens ──────────────────────────


def test_unknown_model_zero_cost_keeps_tokens():
    usage = _usage(input_tokens=1000, output_tokens=500, cache_read_input_tokens=200)
    result = cost_usd(usage, "totally-made-up-model")

    assert isinstance(result, CostBreakdown)
    assert result.priced is False
    assert result.source == "unpriced"
    # Every cost zeroed.
    assert result.input_cost == 0.0
    assert result.output_cost == 0.0
    assert result.cache_read_cost == 0.0
    assert result.cache_write_cost == 0.0
    assert result.total_cost == 0.0
    # Tokens retained.
    assert result.input_tokens == 1000
    assert result.output_tokens == 500
    assert result.cache_read_tokens == 200


def test_synthetic_model_unpriced_keeps_tokens():
    usage = _usage(input_tokens=42, output_tokens=7)
    result = cost_usd(usage, "<synthetic>")

    assert result.priced is False
    assert result.source == "unpriced"
    assert result.total_cost == 0.0
    assert result.input_tokens == 42
    assert result.output_tokens == 7


def test_none_model_unpriced():
    result = cost_usd(_usage(input_tokens=10), None)
    assert result.priced is False
    assert result.total_cost == 0.0
    assert result.input_tokens == 10


# ── Base input/output pricing ───────────────────────────────────────────────


def test_basic_input_output_cost_opus():
    usage = _usage(input_tokens=1_000_000, output_tokens=1_000_000)
    result = cost_usd(usage, "claude-opus-4-8")

    assert result.priced is True
    assert result.source == "exact"  # no cache writes -> nothing approximated
    assert result.input_cost == pytest.approx(5.0)
    assert result.output_cost == pytest.approx(25.0)
    assert result.total_cost == pytest.approx(30.0)


@pytest.mark.parametrize(
    ("model", "in_rate", "out_rate"),
    [
        ("claude-opus-4-8", 5.0, 25.0),
        ("claude-opus-4-7", 5.0, 25.0),
        ("claude-opus-4-6", 5.0, 25.0),
        ("claude-sonnet-4-6", 3.0, 15.0),
        ("claude-haiku-4-5", 1.0, 5.0),
        ("fable-5", 10.0, 50.0),
    ],
)
def test_pricing_table_matches_skill_grounding(model, in_rate, out_rate):
    assert PRICING[model]["input"] == in_rate
    assert PRICING[model]["output"] == out_rate


def test_cache_read_uses_discount_multiplier():
    usage = _usage(cache_read_input_tokens=1_000_000)
    result = cost_usd(usage, "claude-sonnet-4-6")
    # base input 3.0 * CACHE_READ (0.10) = 0.30 per Mtok
    assert result.cache_read_cost == pytest.approx(3.0 * CACHE_READ)
    assert result.cache_read_cost == pytest.approx(0.30)
    assert result.total_cost == pytest.approx(0.30)


# ── Cache-write TTL split (exact) vs flat fallback (approximated) ────────────


def test_ttl_split_exact_prices_each_bucket():
    usage = _usage(
        cache_creation={
            "ephemeral_5m_input_tokens": 1_000_000,
            "ephemeral_1h_input_tokens": 1_000_000,
        },
    )
    result = cost_usd(usage, "claude-haiku-4-5")  # base input 1.0

    assert result.source == "exact"
    assert result.cache_write_5m_tokens == 1_000_000
    assert result.cache_write_1h_tokens == 1_000_000
    expected = 1.0 * CACHE_WRITE_5M + 1.0 * CACHE_WRITE_1H  # 1.25 + 2.00
    assert result.cache_write_cost == pytest.approx(expected)
    assert result.cache_write_cost == pytest.approx(3.25)


def test_flat_cache_write_falls_back_to_5m_and_is_approximated():
    usage = _usage(cache_creation_input_tokens=1_000_000)
    result = cost_usd(usage, "claude-haiku-4-5")  # base input 1.0

    assert result.source == "approximated"
    assert result.cache_write_5m_tokens == 1_000_000
    assert result.cache_write_1h_tokens == 0
    # Whole amount priced at the 5m rate.
    assert result.cache_write_cost == pytest.approx(1.0 * CACHE_WRITE_5M)
    assert result.cache_write_cost == pytest.approx(1.25)


def test_ttl_split_differs_from_flat_fallback():
    """Same total cache-creation tokens, different cost depending on whether the
    transcript carries the per-TTL split."""
    split = cost_usd(
        _usage(
            cache_creation={
                "ephemeral_5m_input_tokens": 500_000,
                "ephemeral_1h_input_tokens": 500_000,
            }
        ),
        "claude-haiku-4-5",
    )
    flat = cost_usd(_usage(cache_creation_input_tokens=1_000_000), "claude-haiku-4-5")

    assert split.source == "exact"
    assert flat.source == "approximated"
    # 1h premium makes the exact split strictly more expensive than the all-5m
    # approximation here.
    assert split.cache_write_cost > flat.cache_write_cost
    assert split.cache_write_cost == pytest.approx(0.5 * CACHE_WRITE_5M + 0.5 * CACHE_WRITE_1H)


def test_partial_ttl_split_is_exact_with_missing_bucket_zero():
    usage = _usage(cache_creation={"ephemeral_1h_input_tokens": 1_000_000})
    result = cost_usd(usage, "claude-haiku-4-5")
    assert result.source == "exact"
    assert result.cache_write_5m_tokens == 0
    assert result.cache_write_1h_tokens == 1_000_000
    assert result.cache_write_cost == pytest.approx(CACHE_WRITE_1H)


def test_no_cache_activity_is_exact_zero():
    result = cost_usd(_usage(input_tokens=100), "claude-opus-4-8")
    assert result.source == "exact"
    assert result.cache_write_cost == 0.0


# ── Alias canonicalization (before lookup) ──────────────────────────────────


def test_canonicalize_explicit_alias():
    assert canonicalize("claude-opus-4-8-20260514") == "claude-opus-4-8"
    assert canonicalize("opus") == "claude-opus-4-8"
    assert canonicalize("sonnet") == "claude-sonnet-4-6"


def test_canonicalize_strips_date_suffix():
    assert canonicalize("claude-sonnet-4-6-20260601") == "claude-sonnet-4-6"


def test_canonicalize_passthrough_and_empty():
    assert canonicalize("claude-opus-4-8") == "claude-opus-4-8"
    assert canonicalize(None) == ""
    assert canonicalize("") == ""


def test_dated_snapshot_prices_like_canonical():
    usage = _usage(input_tokens=1_000_000)
    dated = cost_usd(usage, "claude-opus-4-8-20260514")
    canonical = cost_usd(usage, "claude-opus-4-8")

    assert dated.priced is True
    assert dated.canonical_model == "claude-opus-4-8"
    assert dated.input_cost == pytest.approx(canonical.input_cost)


# ── KAIZEN_PRICING_JSON override ────────────────────────────────────────────


def test_env_override_merges_over_pricing(monkeypatch):
    monkeypatch.setenv(
        "KAIZEN_PRICING_JSON",
        json.dumps({"claude-opus-4-8": {"input": 99.0, "output": 199.0}}),
    )
    result = cost_usd(_usage(input_tokens=1_000_000, output_tokens=1_000_000), "claude-opus-4-8")
    assert result.input_cost == pytest.approx(99.0)
    assert result.output_cost == pytest.approx(199.0)


def test_env_override_can_add_new_model(monkeypatch):
    monkeypatch.setenv(
        "KAIZEN_PRICING_JSON",
        json.dumps({"experimental-9": {"input": 2.0, "output": 4.0}}),
    )
    result = cost_usd(_usage(input_tokens=1_000_000), "experimental-9")
    assert result.priced is True
    assert result.input_cost == pytest.approx(2.0)


def test_malformed_env_override_is_ignored(monkeypatch):
    monkeypatch.setenv("KAIZEN_PRICING_JSON", "{not valid json")
    result = cost_usd(_usage(input_tokens=1_000_000), "claude-opus-4-8")
    # Falls back to the built-in table.
    assert result.input_cost == pytest.approx(5.0)


# ── Misc invariants ─────────────────────────────────────────────────────────


def test_total_is_sum_of_categories():
    usage = _usage(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_input_tokens=1_000_000,
        cache_creation={
            "ephemeral_5m_input_tokens": 1_000_000,
            "ephemeral_1h_input_tokens": 1_000_000,
        },
    )
    r = cost_usd(usage, "claude-opus-4-8")
    assert r.total_cost == pytest.approx(
        r.input_cost + r.output_cost + r.cache_read_cost + r.cache_write_cost
    )


def test_to_dict_is_plain_dict():
    d = cost_usd(_usage(input_tokens=1), "claude-opus-4-8").to_dict()
    assert isinstance(d, dict)
    assert d["model"] == "claude-opus-4-8"
    assert d["priced"] is True


def test_pricing_as_of_is_dated_constant():
    assert isinstance(PRICING_AS_OF, str)
    assert PRICING_AS_OF


def test_dict_usage_record_supported():
    usage = {"input_tokens": 1_000_000, "output_tokens": 0}
    result = cost_usd(usage, "claude-opus-4-8")
    assert result.input_cost == pytest.approx(5.0)
