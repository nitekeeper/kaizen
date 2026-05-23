"""Tests for scripts/reviewers.py — disjoint reviewer selection for Phase 5b'."""

from __future__ import annotations

import pytest

from scripts.reviewers import InsufficientRosterError, select_reviewers


def test_happy_path_returns_n_disjoint_reviewers():
    roster = ["a", "b", "c", "d", "e", "f"]
    implementers = ["a", "b"]
    result = select_reviewers(roster, implementers, n=3)
    assert len(result) == 3
    assert set(result).isdisjoint(set(implementers))
    assert set(result).issubset(set(roster))


def test_disjointness_invariant_holds():
    roster = ["sec-1", "arch-1", "be-1", "fe-1", "sdet-1"]
    implementers = ["be-1", "fe-1"]
    result = select_reviewers(roster, implementers, n=3)
    for role in result:
        assert role not in implementers


def test_insufficient_roster_when_pool_too_small():
    roster = ["a", "b", "c", "d"]
    implementers = ["a", "b"]
    with pytest.raises(InsufficientRosterError) as excinfo:
        select_reviewers(roster, implementers, n=3)
    msg = str(excinfo.value)
    assert "3" in msg
    assert "2" in msg
    # Error must list the implementer ids that actually overlapped the roster
    # so callers can spot typo'd implementer ids quickly.
    assert "'a'" in msg
    assert "'b'" in msg


def test_insufficient_roster_when_implementers_equal_roster():
    roster = ["a", "b", "c"]
    implementers = ["a", "b", "c"]
    with pytest.raises(InsufficientRosterError):
        select_reviewers(roster, implementers, n=1)


def test_n_equals_one_returns_single_element():
    roster = ["a", "b", "c"]
    implementers = ["a"]
    result = select_reviewers(roster, implementers, n=1)
    assert len(result) == 1
    assert result[0] in ("b", "c")
    assert result[0] not in implementers


def test_n_less_than_one_raises_value_error():
    roster = ["a", "b", "c"]
    with pytest.raises(ValueError):
        select_reviewers(roster, [], n=0)
    with pytest.raises(ValueError):
        select_reviewers(roster, [], n=-1)


def test_preferred_lenses_ordering():
    roster = [
        "backend-engineer-1",
        "security-engineer-1",
        "frontend-engineer-1",
        "software-architect-1",
        "prompt-engineer-1",
        "sdet-1",
    ]
    implementers: list[str] = []
    result = select_reviewers(
        roster,
        implementers,
        n=4,
        preferred_lenses=["security", "architect", "prompt"],
    )
    assert result[0] == "security-engineer-1"
    assert result[1] == "software-architect-1"
    assert result[2] == "prompt-engineer-1"
    # 4th slot fills from remaining pool in input order → "backend-engineer-1"
    assert result[3] == "backend-engineer-1"


def test_preferred_lenses_with_no_matches_falls_back_to_input_order():
    roster = ["alpha", "beta", "gamma", "delta"]
    result = select_reviewers(
        roster,
        [],
        n=2,
        preferred_lenses=["nonexistent", "alsomissing"],
    )
    assert result == ["alpha", "beta"]


def test_duplicate_handling_preserves_first_occurrence():
    roster = ["a", "b", "a", "c"]
    result = select_reviewers(roster, [], n=3)
    assert result == ["a", "b", "c"]


def test_duplicate_role_in_both_roster_and_implementers():
    # Role "a" appears twice in roster and once in implementers — it must be
    # excluded; the dedup-then-disjoint pipeline returns only the remaining
    # roster roles in first-occurrence order.
    result = select_reviewers(
        roster=["a", "a", "b", "c"],
        implementers=["a"],
        n=2,
    )
    assert result == ["b", "c"]


def test_determinism_same_inputs_same_output():
    roster = ["a", "b", "c", "d", "e"]
    implementers = ["a"]
    r1 = select_reviewers(roster, implementers, n=3, preferred_lenses=["c", "d"])
    r2 = select_reviewers(roster, implementers, n=3, preferred_lenses=["c", "d"])
    assert r1 == r2
