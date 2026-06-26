"""Tests for the canonical tokenmeter foundation types (scripts.tokenmeter_model).

Covers the type contracts the six tokenmeter modules depend on: ``TokenUsage``
(saturating add, per-field max_merge, and the DELIBERATE absence of ``total()``),
``UsageRecord`` (field shape + free-form ``source`` provenance), ``RunStatus``
(the three-member vocabulary + stable string values), and the ``TokenCounter``
Protocol (structural conformance). A final UNIFICATION test proves the seam modules
now import the canonical types — i.e. their ``except ImportError`` mirrors are dead.

Stdlib + pytest only.
"""

from __future__ import annotations

from scripts.tokenmeter_model import (
    SATURATION_MAX,
    RunStatus,
    TokenCounter,
    TokenUsage,
    UsageRecord,
)

# ── TokenUsage: fields / defaults ────────────────────────────────────────────


def test_token_usage_defaults_all_zero():
    u = TokenUsage()
    assert u.input_tokens == 0
    assert u.output_tokens == 0
    assert u.cache_creation_input_tokens == 0
    assert u.cache_read_input_tokens == 0


def test_token_usage_field_order_matches_seam_mirror():
    # Positional construction must match the mirror's field ORDER exactly.
    u = TokenUsage(1, 2, 3, 4)
    assert (u.input_tokens, u.output_tokens) == (1, 2)
    assert (u.cache_creation_input_tokens, u.cache_read_input_tokens) == (3, 4)


def test_token_usage_is_frozen():
    import dataclasses

    import pytest

    u = TokenUsage(1, 2, 3, 4)
    with pytest.raises(dataclasses.FrozenInstanceError):
        u.input_tokens = 99  # type: ignore[misc]


# ── TokenUsage.__add__ (saturating) ──────────────────────────────────────────


def test_add_is_per_field_sum():
    total = TokenUsage(1, 2, 3, 4) + TokenUsage(10, 20, 30, 40)
    assert total == TokenUsage(11, 22, 33, 44)


def test_add_saturates_at_ceiling():
    saturated = TokenUsage(input_tokens=SATURATION_MAX) + TokenUsage(input_tokens=10)
    assert saturated.input_tokens == SATURATION_MAX
    # Untouched fields stay zero — saturation is per-field.
    assert saturated.output_tokens == 0


def test_add_rejects_non_token_usage():
    # NotImplemented → Python raises TypeError for the unsupported operand pair.
    import pytest

    with pytest.raises(TypeError):
        _ = TokenUsage(1, 0, 0, 0) + 5  # type: ignore[operator]


# ── TokenUsage.max_merge ─────────────────────────────────────────────────────


def test_max_merge_takes_per_field_max():
    a = TokenUsage(input_tokens=5, output_tokens=50, cache_read_input_tokens=7)
    b = TokenUsage(input_tokens=8, output_tokens=30, cache_read_input_tokens=4)
    merged = a.max_merge(b)
    assert merged.input_tokens == 8  # max(5, 8)
    assert merged.output_tokens == 50  # max(50, 30)
    assert merged.cache_read_input_tokens == 7  # max(7, 4)
    # max_merge is symmetric.
    assert a.max_merge(b) == b.max_merge(a)


def test_max_merge_is_not_a_sum():
    a = TokenUsage(output_tokens=5)
    b = TokenUsage(output_tokens=50)
    # The whole point: streaming partials merge to the MAX (50), never the sum (55).
    assert a.max_merge(b).output_tokens == 50


# ── DELIBERATELY no total() ──────────────────────────────────────────────────


def test_token_usage_has_no_total_method():
    # cache_read dwarfs token COUNT while output dominates COST — a single summed
    # "total" is a lie, so it must never exist on TokenUsage.
    assert not hasattr(TokenUsage, "total")
    assert not hasattr(TokenUsage(1, 2, 3, 4), "total")


# ── UsageRecord ──────────────────────────────────────────────────────────────


def test_usage_record_defaults():
    rec = UsageRecord(usage=TokenUsage(1, 2, 3, 4))
    assert rec.usage == TokenUsage(1, 2, 3, 4)
    assert rec.session_id is None
    assert rec.dedup_key is None
    assert rec.source == ""
    assert rec.agent_label is None
    assert rec.is_sidechain is False
    assert rec.kept_but_suspect is False


def test_usage_record_source_is_free_form_provenance():
    # Seam B fills `source` with the transcript file PATH (provenance), not a
    # measured/approximated tag — any string must be accepted, never validated.
    rec = UsageRecord(usage=TokenUsage(), source="/x/projects/proj/sess-1.jsonl")
    assert rec.source == "/x/projects/proj/sess-1.jsonl"


def test_usage_record_is_frozen():
    import dataclasses

    import pytest

    rec = UsageRecord(usage=TokenUsage())
    with pytest.raises(dataclasses.FrozenInstanceError):
        rec.is_sidechain = True  # type: ignore[misc]


# ── RunStatus ────────────────────────────────────────────────────────────────


def test_run_status_members_and_values():
    assert {m.name for m in RunStatus} == {"SUCCESS", "SUCCESS_ZERO_COST", "FAILURE"}
    # SUCCESS / FAILURE keep the exact values the result.py mirror used.
    assert RunStatus.SUCCESS.value == "success"
    assert RunStatus.FAILURE.value == "failure"
    assert RunStatus.SUCCESS_ZERO_COST.value == "success_zero_cost"


def test_run_status_members_distinct():
    assert RunStatus.SUCCESS is not RunStatus.FAILURE
    assert RunStatus.SUCCESS is not RunStatus.SUCCESS_ZERO_COST


# ── TokenCounter Protocol (structural) ───────────────────────────────────────


class _Counter:
    source = "measured"

    def count(self, text: str) -> int | None:
        return len(text) if text else 0


class _NotACounter:
    def tally(self, text: str) -> int:
        return len(text)


def test_token_counter_protocol_structural_conformance():
    assert isinstance(_Counter(), TokenCounter)  # has count(text)
    assert not isinstance(_NotACounter(), TokenCounter)  # no count(text)


def test_token_counter_count_returns_int_or_none():
    counter = _Counter()
    assert counter.count("abcd") == 4
    assert counter.count("") == 0


# ── UNIFICATION: the seam mirrors are now dead branches ──────────────────────


def test_seam_modules_import_the_canonical_types():
    import scripts.tokenmeter_result as result
    import scripts.tokenmeter_static as static
    import scripts.tokenmeter_transcript as transcript

    # If the `try: from scripts.tokenmeter_model import ...` resolved, these are the
    # SAME objects — proving each module's `except ImportError` mirror is now dead.
    assert transcript.TokenUsage is TokenUsage
    assert transcript.UsageRecord is UsageRecord
    assert result.TokenUsage is TokenUsage
    assert result.RunStatus is RunStatus
    assert static.TokenCounter is TokenCounter
