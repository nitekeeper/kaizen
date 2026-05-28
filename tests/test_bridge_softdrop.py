"""Tests for scripts/bridge_softdrop.py — the soft-drop record shape.

These pin the bridge<->synthesis-template contract: the exact sentinel
literal, the record shape consumed by the quorum / send_message_many unwrap,
and the flag-is-authoritative invariant.
"""

from scripts.bridge_softdrop import (
    SOFT_DROP_SENTINEL,
    is_soft_drop_record,
    make_soft_drop_record,
)


def test_sentinel_exact_literal():
    """Pin the exact sentinel string so the bridge<->template contract cannot
    drift. The synthesis dispatch template hard-codes this prefix; if someone
    edits it here, this test must fail to force a paired template update."""
    assert SOFT_DROP_SENTINEL == "<SOFT-DROPPED:"


def test_record_shape_has_all_three_keys():
    rec = make_soft_drop_record(2, "backend-engineer-1", "row never reached ready")
    assert set(rec) == {"response", "soft_dropped", "to"}


def test_response_is_str_opening_with_sentinel():
    """send_message_many requires a `response` string; a soft-drop must satisfy
    that (so the unwrap does not raise) and carry the prose sentinel prefix."""
    rec = make_soft_drop_record(0, "data-engineer-1", "soft-timeout exceeded")
    assert isinstance(rec["response"], str)
    assert rec["response"].startswith(SOFT_DROP_SENTINEL)


def test_response_carries_reason_recipient_and_idx():
    rec = make_soft_drop_record(5, "sdet-1", "no ready transition")
    assert "no ready transition" in rec["response"]
    assert "sdet-1" in rec["response"]
    assert "idx=5" in rec["response"]


def test_soft_dropped_flag_is_true_boolean():
    """The flag is the authoritative signal — assert it is literally True,
    not merely truthy."""
    rec = make_soft_drop_record(1, "reviewer-1", "silence")
    assert rec["soft_dropped"] is True


def test_to_preserves_recipient():
    rec = make_soft_drop_record(3, "ai-safety-researcher-1", "silence")
    assert rec["to"] == "ai-safety-researcher-1"


def test_is_soft_drop_record_true_for_factory_output():
    rec = make_soft_drop_record(0, "x", "silence")
    assert is_soft_drop_record(rec) is True


def test_is_soft_drop_record_false_for_real_response():
    """A genuine teammate response (no soft_dropped flag) is not a soft-drop —
    even if its prose happened to contain the sentinel text."""
    real = {"response": f"my analysis mentions {SOFT_DROP_SENTINEL} as a token"}
    assert is_soft_drop_record(real) is False


def test_is_soft_drop_record_false_for_non_dict_and_falsey_flag():
    assert is_soft_drop_record(None) is False
    assert is_soft_drop_record("string") is False
    assert is_soft_drop_record({"response": "ok", "soft_dropped": False}) is False
