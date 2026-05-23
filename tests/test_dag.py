"""Tests for scripts/dag.py — Phase 3 Action Items DAG validation gates."""

from __future__ import annotations

import pytest

from scripts.dag import (
    CycleDetectedError,
    FileContentionError,
    OrphanDependencyError,
    UnsatisfiableReadsError,
    topological_waves,
    validate_dag,
)


def _item(
    item_id: str,
    *,
    touches: list[str] | None = None,
    reads: list[str] | None = None,
    depends_on: list[str] | None = None,
    wave: int = 1,
    owner: str = "backend-engineer-1",
    description: str = "",
) -> dict:
    """Tiny factory for well-formed Action Item dicts in tests."""
    return {
        "id": item_id,
        "touches": touches or [],
        "reads": reads or [],
        "depends_on": depends_on or [],
        "wave": wave,
        "owner": owner,
        "description": description or f"action {item_id}",
    }


def test_topological_waves_simple_chain():
    items = [
        _item("A"),
        _item("B", depends_on=["A"]),
        _item("C", depends_on=["B"]),
    ]
    assert topological_waves(items) == (("A",), ("B",), ("C",))


def test_topological_waves_parallel():
    items = [
        _item("A"),
        _item("B"),
        _item("C", depends_on=["A", "B"]),
    ]
    waves = topological_waves(items)
    assert waves == (("A", "B"), ("C",))


def test_topological_waves_diamond():
    items = [
        _item("A"),
        _item("B", depends_on=["A"]),
        _item("C", depends_on=["A"]),
        _item("D", depends_on=["B", "C"]),
    ]
    waves = topological_waves(items)
    assert waves == (("A",), ("B", "C"), ("D",))


def test_topological_waves_detects_cycle():
    items = [
        _item("A", depends_on=["B"]),
        _item("B", depends_on=["A"]),
    ]
    with pytest.raises(CycleDetectedError) as excinfo:
        topological_waves(items)
    msg = str(excinfo.value)
    assert "A" in msg
    assert "B" in msg


def test_validate_dag_happy_path():
    items = [
        _item("A", touches=["x.py"]),
        _item("B", touches=["y.py"], reads=["x.py"], depends_on=["A"]),
    ]
    result = validate_dag(items)
    assert result.ok is True
    assert result.waves == (("A",), ("B",))
    assert result.errors == ()


def test_validate_dag_detects_cycle():
    items = [
        _item("A", depends_on=["B"]),
        _item("B", depends_on=["A"]),
    ]
    result = validate_dag(items)
    assert result.ok is False
    assert result.waves == ()
    assert len(result.errors) == 1
    assert isinstance(result.errors[0], CycleDetectedError)


def test_validate_dag_detects_file_contention():
    items = [
        _item("A", touches=["x.py"]),
        _item("B", touches=["x.py"]),
    ]
    result = validate_dag(items)
    assert result.ok is False
    contention_errors = [e for e in result.errors if isinstance(e, FileContentionError)]
    assert len(contention_errors) == 1
    msg = str(contention_errors[0])
    assert "A" in msg
    assert "B" in msg
    assert "x.py" in msg


def test_validate_dag_unsatisfiable_reads_with_no_existing_files():
    items = [
        _item("A", reads=["nonexistent.py"]),
    ]
    result = validate_dag(items)
    assert result.ok is False
    read_errors = [e for e in result.errors if isinstance(e, UnsatisfiableReadsError)]
    assert len(read_errors) == 1
    assert "nonexistent.py" in str(read_errors[0])
    assert "A" in str(read_errors[0])


def test_validate_dag_satisfiable_reads_via_existing_files():
    items = [
        _item("A", reads=["existing.py"]),
    ]
    result = validate_dag(items, existing_files=frozenset({"existing.py"}))
    assert result.ok is True
    assert result.errors == ()


def test_validate_dag_satisfiable_reads_via_earlier_wave_touches():
    items = [
        _item("A", touches=["x.py"]),
        _item("B", reads=["x.py"], depends_on=["A"]),
    ]
    result = validate_dag(items)
    assert result.ok is True
    assert result.errors == ()


def test_validate_dag_orphan_dependency():
    items = [
        _item("A", depends_on=["AI-999"]),
    ]
    result = validate_dag(items)
    assert result.ok is False
    orphan_errors = [e for e in result.errors if isinstance(e, OrphanDependencyError)]
    assert len(orphan_errors) == 1
    assert "AI-999" in str(orphan_errors[0])
    assert "A" in str(orphan_errors[0])


def test_validate_dag_collects_multiple_errors():
    # File contention (A and B both touch x.py in wave 1) + orphan dep
    # (C depends on a missing id). Both errors must be reported, not just
    # the first.
    items = [
        _item("A", touches=["x.py"]),
        _item("B", touches=["x.py"]),
        _item("C", depends_on=["AI-MISSING"]),
    ]
    result = validate_dag(items)
    assert result.ok is False
    assert any(isinstance(e, FileContentionError) for e in result.errors)
    assert any(isinstance(e, OrphanDependencyError) for e in result.errors)
    # At least 2 distinct errors.
    assert len(result.errors) >= 2


def test_validate_dag_raises_on_malformed_item():
    items = [
        {"touches": [], "reads": [], "depends_on": [], "wave": 1},  # missing id
    ]
    with pytest.raises(ValueError) as excinfo:
        validate_dag(items)
    msg = str(excinfo.value)
    assert "id" in msg
    # Must list the required keys so the agent can fix the source proposal.
    assert "touches" in msg
    assert "reads" in msg
    assert "depends_on" in msg


def test_topological_waves_self_loop_detected():
    # A depends on itself — single-node cycle. Must surface as CycleDetectedError
    # naming A (NOT as an orphan dep nor as a silent wave-skip).
    items = [_item("A", depends_on=["A"])]
    with pytest.raises(CycleDetectedError) as excinfo:
        topological_waves(items)
    assert "A" in str(excinfo.value)


def test_topological_waves_disconnected_components():
    # Two independent chains A->B and C->D — both wave-1 roots collapse into
    # one frame; both wave-2 leaves likewise.
    items = [
        _item("A"),
        _item("B", depends_on=["A"]),
        _item("C"),
        _item("D", depends_on=["C"]),
    ]
    waves = topological_waves(items)
    assert waves == (("A", "C"), ("B", "D"))


def test_validate_dag_rejects_non_string_id():
    items = [
        {
            "id": 42,
            "touches": [],
            "reads": [],
            "depends_on": [],
            "wave": 1,
            "owner": "x",
            "description": "x",
        },
    ]
    with pytest.raises(ValueError) as excinfo:
        validate_dag(items)
    msg = str(excinfo.value)
    assert "id" in msg
    assert "str" in msg
    assert "int" in msg


def test_validate_dag_rejects_non_string_element():
    # A stray int in touches would otherwise quietly slip into the
    # produced_so_far set and the file_to_items dict, producing meaningless
    # downstream errors.
    items = [_item("A", touches=["x.py", 42])]  # type: ignore[list-item]
    with pytest.raises(ValueError) as excinfo:
        validate_dag(items)
    msg = str(excinfo.value)
    assert "touches" in msg
    assert "int" in msg
    assert "42" in msg


def test_validate_dag_rejects_intra_item_duplicate():
    # touches=['x.py','x.py'] is a shape bug — must raise ValueError
    # NOT a FileContentionError. (Without this guard, gate 2 would falsely
    # report 'x.py touched by both A and A'.)
    items = [_item("A", touches=["x.py", "x.py"])]
    with pytest.raises(ValueError) as excinfo:
        validate_dag(items)
    msg = str(excinfo.value)
    assert "touches" in msg
    assert "duplicate" in msg
    assert "x.py" in msg
    # Crucially, it's NOT a FileContentionError (which would mask the real bug).
    assert not isinstance(excinfo.value, FileContentionError)
