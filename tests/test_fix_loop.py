"""Tests for scripts/fix_loop.py — Phase 5b' fix-loop iteration tracker."""

from __future__ import annotations

import json

import pytest

from scripts.abandonment import VALID_PHASES, VALID_REASONS
from scripts.fix_loop import (
    MAX_ITERATIONS,
    Finding,
    FixLoopExhausted,
    FixLoopState,
    build_abandonment_outcome,
    record_findings,
    should_continue,
    start_iteration,
)


def _f(fid: str, severity: str, reviewer: str = "sec-1") -> Finding:
    return Finding(
        finding_id=fid,
        reviewer=reviewer,
        severity=severity,
        finding=f"problem in thing {fid}",
        file_line=f"file_{fid}.py:10",
    )


def test_finding_rejects_invalid_severity():
    # A reviewer typo like "blocer" would silently classify as non-blocking and
    # exit the fix loop early — Finding must reject it at construction time.
    with pytest.raises(ValueError) as excinfo:
        Finding(
            finding_id="F-1",
            reviewer="sec-1",
            severity="blocer",
            finding="problem",
            file_line="a.py:1",
        )
    msg = str(excinfo.value)
    # The rejected value AND the valid-severity menu must appear in the message.
    assert "'blocer'" in msg
    for valid in ("blocker", "major", "minor", "nit"):
        assert valid in msg


def test_start_iteration_increments():
    state = FixLoopState()
    assert start_iteration(state) == 1
    assert start_iteration(state) == 2
    assert start_iteration(state) == 3
    assert state.iteration == 3


def test_start_iteration_raises_after_max():
    state = FixLoopState()
    for expected in range(1, MAX_ITERATIONS + 1):
        assert start_iteration(state) == expected
    assert state.iteration == MAX_ITERATIONS
    with pytest.raises(FixLoopExhausted):
        start_iteration(state)


def test_should_continue_true_when_blocker_remains():
    # Drive via the public API so record_findings is exercised end-to-end.
    state = FixLoopState()
    start_iteration(state)
    record_findings(state, [_f("F-1", "blocker")])
    assert state.history == [[_f("F-1", "blocker")]]
    assert should_continue(state) is True


def test_should_continue_true_when_major_remains():
    state = FixLoopState(iteration=1, history=[[_f("F-1", "major")]])
    assert should_continue(state) is True


def test_should_continue_false_when_only_minor_remains():
    state = FixLoopState(
        iteration=1,
        history=[[_f("F-1", "minor"), _f("F-2", "nit")]],
    )
    assert should_continue(state) is False


def test_should_continue_false_when_pm_accepts():
    state = FixLoopState(
        iteration=2,
        history=[[_f("F-1", "blocker"), _f("F-2", "blocker")]],
    )
    assert should_continue(state, pm_accepts_remaining=True) is False


def test_should_continue_false_when_exhausted():
    # iteration == MAX_ITERATIONS → stop (start_iteration is the raiser).
    state = FixLoopState(
        iteration=MAX_ITERATIONS,
        history=[[_f("F-1", "blocker")] for _ in range(MAX_ITERATIONS)],
    )
    assert should_continue(state) is False


def test_should_continue_empty_findings_returns_false():
    state = FixLoopState(iteration=2, history=[[_f("F-1", "blocker")], []])
    assert should_continue(state) is False


def test_build_abandonment_outcome_shape():
    state = FixLoopState(
        iteration=5,
        history=[
            [],
            [_f("F-1", "blocker", reviewer="sec-1")],
            [_f("F-1", "blocker", reviewer="sec-1")],
            [_f("F-1", "blocker", reviewer="sec-1")],
            [
                _f("F-1", "blocker", reviewer="sec-1"),
                _f("F-2", "major", reviewer="arch-1"),
            ],
        ],
    )
    outcome = build_abandonment_outcome(state, subject="subj", participants=["a", "b"])

    expected_keys = {
        "status",
        "subject",
        "participants",
        "phase_reached",
        "reason",
        "detail",
        "artifacts",
        "review_iteration_count",
        "unresolved_findings",
        "convergence_summary",
        "reviewer_attribution",
    }
    assert set(outcome.keys()) == expected_keys
    assert len(expected_keys) == 11

    assert outcome["status"] == "abandoned"
    assert outcome["subject"] == "subj"
    assert outcome["participants"] == ["a", "b"]
    assert outcome["artifacts"] == []
    assert outcome["review_iteration_count"] == 5
    assert isinstance(outcome["unresolved_findings"], list)
    assert isinstance(outcome["reviewer_attribution"], dict)

    # Findings must be JSON-serialisable (no dataclass leakage).
    json.dumps(outcome["unresolved_findings"])
    json.dumps(outcome["reviewer_attribution"])

    # Each finding dict has the 5 documented keys.
    for f in outcome["unresolved_findings"]:
        assert set(f.keys()) == {"finding_id", "reviewer", "severity", "finding", "file_line"}


def test_build_abandonment_outcome_uses_canonical_enums():
    state = FixLoopState(iteration=5, history=[[], [], [], [], [_f("F-1", "blocker")]])
    outcome = build_abandonment_outcome(state, subject=None, participants=[])
    assert outcome["phase_reached"] == "review"
    assert outcome["reason"] == "review_unrecoverable"
    # Cross-module invariant — both literals must be in the canonical frozensets.
    assert outcome["phase_reached"] in VALID_PHASES
    assert outcome["reason"] in VALID_REASONS


def test_build_abandonment_outcome_convergence_summary_mentions_persistent_findings():
    persistent = _f("F-1", "blocker", reviewer="sec-1")
    state = FixLoopState(
        iteration=5,
        history=[
            [],
            [persistent],
            [persistent],
            [persistent],
            [persistent],
        ],
    )
    outcome = build_abandonment_outcome(state, subject="s", participants=["a"])
    summary = outcome["convergence_summary"]
    # The summary should surface that F-1 recurred across rounds.
    assert "F-1" in summary or "persistent" in summary.lower()
