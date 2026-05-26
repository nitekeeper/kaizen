"""F3 fire-order invariant tests.

F3 (CLAUDE.md, ``During cycle``):
    ``record_cycle_success`` / ``record_cycle_abandoned`` MUST fire
    AFTER ``commit_cycle`` and BEFORE ``push_branch``; the PR title
    renders from the ``cycles`` table, not the run row.

Originating incident: run 19 / PR#30 — the cycle's ``record_cycle_*``
write was reordered after ``push_branch``, so the bundled-PR title
rendered ``0 succeeded`` because the cycles table was still empty at
PR-render time.

This test encodes the **ordering rule generically** (per backend-eng
caveat C3 / ai-safety BLOCKING-2). It does NOT pin a single trace; it
asserts three independent invariants against the captured call log:

  (a) ``commit_cycle`` precedes every same-cycle ``record_cycle_*``
      (success cycles must commit before they record);
  (b) every ``record_cycle_*`` precedes ``push_branch``;
  (c) no ``record_cycle_*`` happens BEFORE the first ``commit_cycle``
      for that cycle's window.

Belt-and-suspenders: the four F3-relevant call sites
(``cycle_git.commit_cycle``, ``cycle.record_cycle_success``,
``cycle.record_cycle_abandoned``, ``cycle_git.push_branch``) are
attached to a single ``MagicMock`` parent so ``parent.mock_calls``
preserves the global ordering across the whole run.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import scripts.cycle as cycle_mod
import scripts.cycle_git as cycle_git_mod
import scripts.run as run_mod
from scripts.migrate import MIGRATIONS_DIR, apply_migrations
from scripts.project import create_project
from scripts.run import orchestrate_run

# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path) -> str:
    db_path = str(tmp_path / "kaizen.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    return db_path


@pytest.fixture
def project(db) -> dict:
    return create_project(
        db,
        git_url="https://github.com/owner/repo.git",
        name="repo",
        base_branch="main",
        test_command="pytest",
        read_paths=[],
        expert_roster=[],
        language="python",
    )


@pytest.fixture
def fire_order_recorder(monkeypatch, tmp_path):
    """Attach all F3-relevant call sites to one MagicMock parent.

    Returns the parent ``recorder``; ``recorder.mock_calls`` is the
    ordered, cross-cycle call log used by every test in this file.

    Side-effects:
      * stubs ``clone_repo``, ``seed_all``, ``create_branch`` so the
        orchestrator runs against a tmp path without touching real git;
      * spies on ``commit_cycle`` and ``push_branch`` (no-ops with the
        MagicMock as side effect);
      * spies on ``record_cycle_success`` / ``record_cycle_abandoned``
        but **delegates to the real implementation** so the DB rows
        actually exist (the orchestrator dereferences them downstream).
    """
    recorder = MagicMock()

    # Orchestrator side-effects we don't want — clone, seed, branch.
    monkeypatch.setattr(run_mod, "kaizen_root", lambda: tmp_path)

    import scripts.clone as clone_mod

    monkeypatch.setattr(
        clone_mod,
        "clone_repo",
        lambda url, dest, branch: dest.mkdir(parents=True, exist_ok=True),
    )

    import scripts.seed_atelier_in_clone as seed_mod

    monkeypatch.setattr(seed_mod, "seed_all", lambda d: None)

    monkeypatch.setattr(
        cycle_git_mod,
        "create_branch",
        lambda d, subj: f"kaizen/{(subj or 'pm-directed').replace(' ', '-')}-2026-05-26-0357",
    )

    # Spy: commit_cycle. The real impl shells out to git; replace with a
    # no-op MagicMock so the test runs without a working tree.
    spy_commit = MagicMock(return_value=None)
    monkeypatch.setattr(cycle_git_mod, "commit_cycle", spy_commit)
    recorder.attach_mock(spy_commit, "commit_cycle")

    # Spy: push_branch. Same reasoning.
    spy_push = MagicMock(return_value=None)
    monkeypatch.setattr(cycle_git_mod, "push_branch", spy_push)
    recorder.attach_mock(spy_push, "push_branch")

    # Spy: record_cycle_success / record_cycle_abandoned.
    # The orchestrator needs the returned dict (cycle_id is used by
    # process_abandonment), so wrap the real function as side_effect.
    real_success = cycle_mod.record_cycle_success
    real_abandoned = cycle_mod.record_cycle_abandoned

    spy_success = MagicMock(side_effect=real_success)
    spy_abandoned = MagicMock(side_effect=real_abandoned)
    monkeypatch.setattr(cycle_mod, "record_cycle_success", spy_success)
    monkeypatch.setattr(cycle_mod, "record_cycle_abandoned", spy_abandoned)
    recorder.attach_mock(spy_success, "record_cycle_success")
    recorder.attach_mock(spy_abandoned, "record_cycle_abandoned")

    return recorder


def _make_contract_executor():
    """Return a fake ``cycle_executor`` that mirrors the real contract.

    Real executors (team_executor, internal/cycle/SKILL.md prose) call
    ``cycle_git.commit_cycle`` BEFORE returning status='success'.
    Abandoned cycles do NOT call commit_cycle. Cycle 2 in this fake
    abandons to exercise the mixed-outcome path.
    """

    def fake_executor(clone_dir, proj, run_row, cycle_n):
        if cycle_n == 2:
            return {
                "status": "abandoned",
                "phase_reached": "meeting",
                "reason": "no_consensus",
                "detail": "agents disagreed",
                "participants": ["pm"],
                "artifacts": [],
            }

        # Late import so monkeypatch's spy is the binding we see.
        from scripts.cycle_git import commit_cycle

        commit_cycle(
            clone_dir,
            cycle_n,
            ["d1"],
            ["pm"],
            1,
            "t",
            "docs/x.md",
        )
        return {
            "status": "success",
            "commit_sha": f"sha-{cycle_n}",
            "minutes_memex_slug": None,
        }

    return fake_executor


def _names(recorder) -> list[str]:
    """Flatten ``recorder.mock_calls`` to ordered attribute names."""
    return [c[0] for c in recorder.mock_calls]


# ── (a) commit_cycle precedes same-cycle record_cycle_* ───────────────────


def test_commit_cycle_precedes_record_for_each_success_cycle(db, project, fire_order_recorder):
    """F3 (a): in every success-cycle window, commit_cycle MUST appear
    before record_cycle_success. The 'window' is the slice of the call
    log between the previous record_cycle_* (or run start) and this
    record_cycle_*. Abandoned cycles legitimately have NO commit_cycle
    in their window."""
    orchestrate_run(
        db_path=db,
        git_url=project["git_url"],
        cycles_requested=3,
        cycle_executor=_make_contract_executor(),
    )
    names = _names(fire_order_recorder)
    last_record_idx = -1
    for i, name in enumerate(names):
        if name == "record_cycle_success":
            window = names[last_record_idx + 1 : i]
            assert "commit_cycle" in window, (
                f"record_cycle_success at index {i} not preceded by "
                f"commit_cycle since previous record_cycle_*; calls={names}"
            )
            last_record_idx = i
        elif name == "record_cycle_abandoned":
            last_record_idx = i


# ── (b) every record_cycle_* precedes push_branch ─────────────────────────


def test_all_records_precede_push_branch(db, project, fire_order_recorder):
    """F3 (b): push_branch MUST fire AFTER every record_cycle_*.
    PR title renders from the cycles table — pushing first means the PR
    opens against a stale/empty count (the exact run-19 / PR#30 bug)."""
    orchestrate_run(
        db_path=db,
        git_url=project["git_url"],
        cycles_requested=3,
        cycle_executor=_make_contract_executor(),
    )
    names = _names(fire_order_recorder)
    push_indices = [i for i, n in enumerate(names) if n == "push_branch"]
    assert len(push_indices) == 1, f"expected exactly one push_branch call; got {names}"
    push_idx = push_indices[0]
    for i, n in enumerate(names):
        if n in ("record_cycle_success", "record_cycle_abandoned"):
            assert i < push_idx, (
                f"record_cycle_* at index {i} fired AFTER push_branch "
                f"(idx {push_idx}); calls={names}"
            )


# ── (c) no record_cycle_* before the first commit_cycle ───────────────────


def test_no_record_before_first_commit_single_success(db, project, fire_order_recorder):
    """F3 (c): for a single success-only cycle, no record_cycle_* may
    appear in the log before commit_cycle does."""
    orchestrate_run(
        db_path=db,
        git_url=project["git_url"],
        cycles_requested=1,
        cycle_executor=_make_contract_executor(),
    )
    names = _names(fire_order_recorder)
    first_commit = next((i for i, n in enumerate(names) if n == "commit_cycle"), None)
    first_record = next(
        (i for i, n in enumerate(names) if n in ("record_cycle_success", "record_cycle_abandoned")),
        None,
    )
    assert first_commit is not None, f"expected commit_cycle; got {names}"
    assert first_record is not None, f"expected record_cycle_*; got {names}"
    assert first_commit < first_record, (
        f"commit_cycle (idx {first_commit}) MUST precede first "
        f"record_cycle_* (idx {first_record}); calls={names}"
    )


# ── Belt-and-suspenders: end-to-end ordering on mixed outcomes ────────────


def test_full_ordering_mixed_success_and_abandoned(db, project, fire_order_recorder):
    """Belt-and-suspenders: across a mixed run (cycles 1 + 3 succeed,
    cycle 2 abandons) we check (a) and (b) together plus the cardinal
    F3 invariant that push_branch is the LAST F3 call in the log."""
    orchestrate_run(
        db_path=db,
        git_url=project["git_url"],
        cycles_requested=3,
        cycle_executor=_make_contract_executor(),
    )
    names = _names(fire_order_recorder)

    # Cardinality: two successful commits, two success records,
    # one abandoned record, one push.
    assert names.count("commit_cycle") == 2, names
    assert names.count("record_cycle_success") == 2, names
    assert names.count("record_cycle_abandoned") == 1, names
    assert names.count("push_branch") == 1, names

    # push_branch is the LAST F3-relevant call.
    assert names[-1] == "push_branch", f"push_branch must be the last F3 call; got {names}"

    # First commit precedes first record_cycle_*.
    first_commit = names.index("commit_cycle")
    first_record = next(i for i, n in enumerate(names) if n.startswith("record_cycle_"))
    assert first_commit < first_record, (
        f"first commit_cycle (idx {first_commit}) must precede first "
        f"record_cycle_* (idx {first_record}); calls={names}"
    )
