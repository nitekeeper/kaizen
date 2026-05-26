"""Regression test for kaizen#69 — Phase 3 close prompt vs DAG validator contract.

Run 36 abandoned at Phase 3 close because the architect followed the
template's guidance — "For each file in `touches`, include any
corresponding test file in `reads`" — and the DAG validator (gate 3
"reads satisfiable", `scripts.dag.UnsatisfiableReadsError`) rejected the
otherwise-sound 8-item plan. New test files being created in the same
wave as their source file are NOT pre-existing on disk and are NOT
produced by an earlier wave, so `reads` is the wrong field — `touches`
is the correct one.

This file pins two contracts so a future regression to either the prompt
or the validator that re-introduces the bug fails loudly:

  Part (a) — prompt wording (`scripts.dispatch_templates.phase_3_close`):
    - MUST NOT instruct architects to put corresponding test files in
      `reads` (the exact phrasing the run-36 architect followed).
    - MUST explicitly tell architects that test files this cycle will
      create belong in `touches`, not `reads`.

  Part (b) — validator behavior (`scripts.dag.validate_dag`):
    - An Action Item with `touches=["src/foo.py", "tests/test_foo.py"]`
      and `reads=[]` MUST validate (the test file is co-produced).
    - An Action Item with `touches=["src/foo.py"]` and
      `reads=["tests/test_foo.py"]` (and no existing_files entry, no
      earlier wave producing it) MUST raise UnsatisfiableReadsError.

Together they would have caught the run-36 abandonment in CI: part (a)
fails against the old template text; part (b) pins the validator's
contract that the corrected prompt now aligns with.
"""

from __future__ import annotations

from scripts.dag import UnsatisfiableReadsError, validate_dag
from scripts.dispatch_templates import phase_3_close

# ---------------------------------------------------------------------------
# Part (a) — prompt wording
# ---------------------------------------------------------------------------


def _rendered_close_prompt() -> str:
    """Render the Phase 3 close prompt with realistic, non-empty inputs."""
    return phase_3_close(
        proposals=[
            {"agent": "backend-engineer-1", "raw": "wire cleanup_orphans into finalize_cycle"},
            {"agent": "agent-systems-architect-1", "raw": "add PaneRole enum, drop TMUX_PANE"},
        ],
        agreements=[
            {"agent": "backend-engineer-1", "raw": "agreed on 3-commit plan"},
            {"agent": "agent-systems-architect-1", "raw": "wave-1/wave-2/wave-3 split is sound"},
        ],
    )


def test_phase_3_close_prompt_does_not_put_test_files_in_reads():
    """The legacy phrasing that caused run-36 to abandon must be gone.

    Anchor: the exact substring the run-36 architect read and followed.
    Any future re-introduction of this guidance (verbatim or paraphrased
    with the same anchor) fails this test immediately.
    """
    msg = _rendered_close_prompt()
    assert "corresponding test file in `reads`" not in msg, (
        "Phase 3 close prompt still contains the legacy guidance that put "
        "test files in `reads`. This is the exact phrasing the run-36 "
        "architect followed, which caused the DAG validator to reject the "
        "cycle (kaizen#69). Move the example into `touches`."
    )


def test_phase_3_close_prompt_states_test_files_belong_in_touches():
    """The corrected guidance must be explicit about touches vs reads.

    Anchor substring "in `touches`, not `reads`" — stable, matches the
    new template wording, and is unambiguous to an LLM reading cold.
    """
    msg = _rendered_close_prompt()
    assert "in `touches`, not `reads`" in msg, (
        "Phase 3 close prompt must explicitly tell architects that test "
        "files being CREATED this cycle belong in `touches`, not `reads`. "
        "See kaizen#69 root cause analysis."
    )


# ---------------------------------------------------------------------------
# Part (b) — validator contract (pins what the corrected prompt aligns with)
# ---------------------------------------------------------------------------


def test_validator_accepts_test_file_co_produced_in_touches():
    """`touches=["src/foo.py","tests/test_foo.py"]`, `reads=[]` MUST validate.

    This is the corrected shape the new prompt teaches: the test file is
    co-produced this cycle, so it belongs in `touches`, and `reads` does
    not need to mention it.
    """
    items = [
        {
            "id": "AI-1",
            "touches": ["src/foo.py", "tests/test_foo.py"],
            "reads": [],
            "depends_on": [],
            "wave": 1,
        }
    ]
    result = validate_dag(items, existing_files=frozenset())
    assert result.ok, f"expected ok=True, got errors={[str(e) for e in result.errors]}"
    assert result.errors == ()
    assert result.waves == (("AI-1",),)


def test_validator_rejects_test_file_in_reads_when_not_yet_produced():
    """`touches=["src/foo.py"]`, `reads=["tests/test_foo.py"]` MUST raise.

    This is the run-36 shape the bad prompt produced: the test file is
    in `reads` but neither pre-exists nor is produced by an earlier wave.
    Gate 3 ("reads satisfiable") must fire UnsatisfiableReadsError.
    """
    items = [
        {
            "id": "AI-1",
            "touches": ["src/foo.py"],
            "reads": ["tests/test_foo.py"],
            "depends_on": [],
            "wave": 1,
        }
    ]
    result = validate_dag(items, existing_files=frozenset())
    assert not result.ok
    assert len(result.errors) == 1
    err = result.errors[0]
    assert isinstance(err, UnsatisfiableReadsError)
    assert "tests/test_foo.py" in str(err)
    assert "AI-1" in str(err)
