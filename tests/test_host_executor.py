"""Tests for the host transport Phase-4 executor (M8a-2a).

Coverage:
  * ``build_engine_tasks`` field-mapping (incl. wave→parallel_group,
    owner→assigned_persona fallback, touches→writes).
  * The pre-bound closures (footgun): briefing/model/escalate render + run
    correctly INSIDE a real ``atelier_engine()`` swap window (where the name
    ``scripts`` resolves to atelier), proving they do NOT import kaizen
    ``scripts.*`` at call time.
  * F7-trailer stripping: the host briefing for a Phase-4 task contains NO
    ``SendMessage(to="team-lead"`` / shutdown-JSON trailer.
  * End-to-end with a FakeCliRunner (NO real ``claude``): a 2-wave Action-Items
    DAG → ``host_cycle_executor`` → the runner-produced files land in the clone
    after worktree merge; success outcome dict matches team mode's shape.
  * Abandonment: an invalid DAG → ``no_consensus`` abandonment dict.

The engine-touching tests SKIP cleanly when atelier (>=1.10.0) is absent so a
fresh box never hard-fails the suite.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from scripts.dispatch_templates import TEAMMATE_REPLY_RULE, phase_4_implementer
from scripts.host_executor import (
    _interpret_engine_results,
    _make_briefing_for,
    _make_escalate_fn,
    _make_model_for,
    build_engine_tasks,
    host_cycle_executor,
)

# ── engine availability gate (skip cleanly on a fresh box) ──────────────────


def _engine_available() -> bool:
    try:
        from scripts.atelier_engine import assert_engine_available

        assert_engine_available()
        return True
    except Exception:
        return False


_HAS_ENGINE = _engine_available()
_SKIP_ENGINE = pytest.mark.skipif(
    not _HAS_ENGINE,
    reason="atelier host engine (>=1.10.0) not available; engine-touching test skipped",
)


# ── build_engine_tasks unit tests ───────────────────────────────────────────


def test_build_engine_tasks_field_mapping():
    items = [
        {
            "id": "AI-1",
            "touches": ["scripts/foo.py"],
            "reads": [],
            "depends_on": [],
            "wave": 1,
            "owner": "backend-engineer-1",
        },
        {
            "id": "AI-3",
            "touches": ["tests/test_foo.py"],
            "reads": ["scripts/foo.py"],
            "depends_on": ["AI-1"],
            "wave": 2,
            "owner": "sdet-1",
        },
    ]
    waves = (("AI-1",), ("AI-3",))
    tasks = build_engine_tasks(items, waves)

    t1, t3 = tasks
    # task_id ← id
    assert t1["task_id"] == "AI-1"
    # parallel_group ← 0-based wave index
    assert t1["parallel_group"] == 0
    assert t3["parallel_group"] == 1
    # writes ← touches (the disjointness key)
    assert t1["writes"] == ["scripts/foo.py"]
    assert t3["writes"] == ["tests/test_foo.py"]
    # reads ← reads verbatim
    assert t3["reads"] == ["scripts/foo.py"]
    # depends_on ← depends_on verbatim
    assert t3["depends_on"] == ["AI-1"]
    # assigned_persona ← owner
    assert t1["assigned_persona"] == "backend-engineer-1"
    assert t3["assigned_persona"] == "sdet-1"
    # phase ← constant
    assert t1["phase"] == "implementation"
    assert t3["phase"] == "implementation"


def test_build_engine_tasks_owner_fallback_to_pm():
    items = [
        {"id": "AI-1", "touches": ["a.py"], "reads": [], "depends_on": [], "wave": 1},
        {
            "id": "AI-2",
            "touches": ["b.py"],
            "reads": [],
            "depends_on": [],
            "wave": 1,
            "owner": "",
        },
    ]
    waves = (("AI-1", "AI-2"),)
    tasks = build_engine_tasks(items, waves, pm="pm-1")
    # missing owner → pm fallback; empty owner → pm fallback
    assert tasks[0]["assigned_persona"] == "pm-1"
    assert tasks[1]["assigned_persona"] == "pm-1"


def test_build_engine_tasks_unknown_id_in_waves_raises():
    items = [{"id": "AI-1", "touches": ["a.py"], "reads": [], "depends_on": [], "wave": 1}]
    with pytest.raises(KeyError, match="not present in any wave"):
        build_engine_tasks(items, ())  # AI-1 absent from waves


# ── closure pre-binding (footgun) tests ─────────────────────────────────────


def _briefing_closure(items):
    from scripts.dag import validate_dag

    v = validate_dag(items, existing_files=frozenset())
    assert v.ok
    group_of = {iid: wi for wi, w in enumerate(v.waves) for iid in w}
    by_id = {i["id"]: i for i in items}
    return (
        _make_briefing_for(by_id, group_of, phase_4_implementer, TEAMMATE_REPLY_RULE),
        v,
    )


def test_briefing_strips_f7_trailer():
    """The host briefing must NOT carry the F7 SendMessage/shutdown trailer."""
    items = [
        {"id": "AI-1", "touches": ["scripts/foo.py"], "reads": [], "depends_on": [], "wave": 1}
    ]
    bf, _ = _briefing_closure(items)
    body = bf({"task_id": "AI-1"}, 1)

    # The F7 directive trailer (SendMessage(to="team-lead") + shutdown JSON) is gone.
    assert 'SendMessage(to="team-lead"' not in body
    assert "shutdown_response" not in body
    assert '{"type":"shutdown_request"' not in body
    # The "Reply format" SendMessage paragraph is gone too (team-mode only).
    assert "IMPORTANT — Reply format:" not in body
    # The terse-rule's "SendMessage / shutdown_response JSON" reference is gone.
    assert "shutdown_response JSON protocol body" not in body
    # But the actual task body + untrusted-input boundary survive.
    assert "implement Action Item AI-1" in body
    assert "Untrusted-input boundary" in body
    # And the host-specific terminal-envelope instruction is appended.
    assert "terminal task_result envelope" in body


@_SKIP_ENGINE
def test_closures_work_inside_engine_window():
    """The closures render/run correctly AFTER entering a real atelier_engine()
    window — proving they reference pre-bound kaizen objects, not an in-window
    ``scripts.*`` import (which would resolve to atelier)."""
    import logging

    from scripts.atelier_engine import assert_engine_available, atelier_engine

    items = [
        {"id": "AI-1", "touches": ["scripts/foo.py"], "reads": [], "depends_on": [], "wave": 1}
    ]
    bf, _ = _briefing_closure(items)
    mf = _make_model_for()
    ef = _make_escalate_fn(logging.getLogger("test"))

    root = assert_engine_available()
    with atelier_engine(root):
        # Inside the window `scripts` == atelier; the closures must still work.
        body = bf({"task_id": "AI-1"}, 1)
        model = mf({"phase": "implementation", "assigned_persona": "be-1"}, 1)
        ef({"task_id": "AI-1", "reason": "probe"})  # must not raise

    assert "implement Action Item AI-1" in body
    assert model == "opus"


def test_model_for_picks_opus_for_implementers():
    mf = _make_model_for()
    assert mf({"phase": "implementation", "assigned_persona": "backend-engineer-1"}, 1) == "opus"


# ── abandonment: invalid DAG → no_consensus ─────────────────────────────────


def test_invalid_dag_cycle_returns_no_consensus(tmp_path):
    """A cyclic DAG → no_consensus abandonment dict (mirrors team mode)."""
    items = [
        {"id": "AI-1", "touches": ["a.py"], "reads": [], "depends_on": ["AI-2"], "wave": 1},
        {"id": "AI-2", "touches": ["b.py"], "reads": [], "depends_on": ["AI-1"], "wave": 1},
    ]
    out = host_cycle_executor(
        action_items=items,
        existing_files=frozenset(),
        clone_dir=tmp_path,
        roster=["backend-engineer-1"],
    )
    assert out["status"] == "abandoned"
    assert out["reason"] == "no_consensus"
    assert out["phase_reached"] == "implementation"
    assert "DAG validation failed" in out["detail"]


def test_invalid_dag_contention_returns_no_consensus(tmp_path):
    """Two items in the same wave touching the same file (non-disjoint writes)
    → no_consensus abandonment."""
    items = [
        {"id": "AI-1", "touches": ["shared.py"], "reads": [], "depends_on": [], "wave": 1},
        {"id": "AI-2", "touches": ["shared.py"], "reads": [], "depends_on": [], "wave": 1},
    ]
    out = host_cycle_executor(
        action_items=items,
        existing_files=frozenset(),
        clone_dir=tmp_path,
        roster=["backend-engineer-1"],
    )
    assert out["status"] == "abandoned"
    assert out["reason"] == "no_consensus"


# ── _interpret_engine_results: the post-engine abandonment branch ───────────
#
# A PURE unit test (no engine, no atelier) of the interpreter that maps the
# engine's per-task results into a kaizen outcome dict. The e2e tests below drive
# the REAL engine, but they cannot deterministically land EVERY failure
# sub-branch (a failed-attempt SENTINEL vs a worker-authored non-`done`
# envelope), so the branch-mutation coverage lives here where both sub-branches
# are hand-built. `is_failed_attempt` is the atelier predicate the production code
# resolves in-window; here we inject a stub so the test needs no atelier import.


class _FakeSentinel:
    """Stand-in for atelier's ``_FailedAttempt`` sentinel — an opaque object that
    is NOT a ``Mapping`` (so the ``status`` read would yield ``None``), matched
    ONLY by the injected ``is_failed_attempt`` stub below."""


def test_interpret_engine_results_flags_both_failure_subbranches():
    """Both abandonment sub-branches in one assertion set:
    * AI-2 returns a valid terminal NON-`done` envelope (``status="blocked"``) →
      flagged by the ``status != "done"`` branch.
    * AI-3 returns a failed-attempt sentinel → flagged by the ``is_failed_attempt``
      guard.
    AI-1 is a clean ``done`` (proves the loop does not over-flag).
    """
    tasks = [
        {"task_id": "AI-1"},
        {"task_id": "AI-2"},
        {"task_id": "AI-3"},
    ]
    sentinel = _FakeSentinel()
    results = [
        {"type": "task_result", "task_id": "AI-1", "status": "done"},
        {"type": "task_result", "task_id": "AI-2", "status": "blocked"},
        sentinel,
    ]

    # Stub predicate: True ONLY for the AI-3 sentinel (identity check), so the
    # `status` read can never accidentally satisfy it.
    def is_failed_attempt(value):
        return value is sentinel

    out = _interpret_engine_results(
        results,
        tasks,
        participants=["backend-engineer-1"],
        subject="test subject",
        is_failed_attempt=is_failed_attempt,
    )

    assert out["status"] == "abandoned"
    assert out["reason"] == "no_consensus"
    assert out["phase_reached"] == "implementation"
    # The detail names BOTH failures, each via its own sub-branch.
    assert "AI-2" in out["detail"]
    assert "status=" in out["detail"]  # the non-`done` branch wording
    assert "AI-3" in out["detail"]
    assert "failed-attempt" in out["detail"]  # the sentinel branch wording
    # AI-1 (clean done) is NOT flagged.
    assert "AI-1" not in out["detail"]


def test_interpret_engine_results_all_done_is_success():
    """Non-vacuity anchor: every task `done` → the success outcome dict (no
    over-flagging). Mirrors team mode's success variant shape."""
    tasks = [{"task_id": "AI-1"}, {"task_id": "AI-2"}]
    results = [
        {"type": "task_result", "task_id": "AI-1", "status": "done"},
        {"type": "task_result", "task_id": "AI-2", "status": "done"},
    ]
    out = _interpret_engine_results(
        results,
        tasks,
        participants=["backend-engineer-1"],
        subject="test subject",
        is_failed_attempt=lambda _v: False,
    )
    assert out["status"] == "success"
    assert out["subject"] == "test subject"
    assert out["commit_sha"] is None  # commit is the orchestrator's job (M8a-2c)
    assert out["participants"] == ["backend-engineer-1"]


# ── end-to-end with a FakeCliRunner (NO real claude) ────────────────────────


class _HonestHostFakeRunner:
    """Stdlib-only honest fake of atelier's CLI runner for the host e2e.

    Duck-typed: carries the ``no_real_process``/``is_fake`` markers so atelier's
    mandatory-sandbox gate exempts it (no real process), records every
    ``(argv, cwd)``, CREATES each task's declared writes in its cwd (so the
    engine's false-`done` guard — a HEAD-relative diff — is satisfied), and
    returns a result dict whose ``structured_output`` is a valid terminal
    envelope matching the dispatched task_id/attempt.
    """

    no_real_process = True
    is_fake = True

    def __init__(
        self,
        writes_by_task: dict[str, list[str]],
        blocked_tasks: frozenset[str] | set[str] | None = None,
    ):
        self.writes_by_task = writes_by_task
        # Task ids for which the worker emits a terminal NON-`done` envelope
        # (``status="blocked"``) and writes NO file — exercises the engine's
        # "a worker reported blocked → cycle abandoned" wiring. A blocked envelope
        # is a VALID terminal envelope (empty `artifacts` is legal for `blocked`),
        # so the engine passes it through to `results` as a Mapping WITHOUT
        # converting it to a failed-attempt sentinel (the #120 false-`done` guard
        # fires ONLY for `status="done"` writers, host_scheduler.py:1216). The
        # pipeline dispatches once, so a `blocked` is terminal-and-cascade, never
        # retried (host_scheduler.py:941-946).
        self.blocked_tasks = frozenset(blocked_tasks or ())
        self.calls: list[dict] = []

    @staticmethod
    def _tid_attempt(argv) -> tuple[str, int]:
        # run_attempt builds the -p line: "Perform task <id> (attempt <n>) ..."
        m = re.search(r"Perform task (\S+) \(attempt (\d+)\)", argv[2])
        assert m is not None, f"unexpected prompt argv: {argv!r}"
        return m.group(1), int(m.group(2))

    async def __call__(self, argv, cwd):
        self.calls.append({"argv": list(argv), "cwd": cwd})
        tid, attempt = self._tid_attempt(argv)
        if tid in self.blocked_tasks:
            # Terminal NON-`done` envelope: writes NOTHING, reports blocked. Empty
            # `artifacts` is legal for `blocked` (envelope_schema.py). The engine
            # returns this verbatim in `results` as a non-`done` Mapping.
            return {
                "usage": {"output_tokens": 5, "input_tokens": 3},
                "total_cost_usd": 0.0,
                "is_error": False,
                "subtype": "success",
                "session_id": "fake-session",
                "num_turns": 1,
                "stop_reason": "end_turn",
                "structured_output": {
                    "type": "task_result",
                    "task_id": tid,
                    "attempt": attempt,
                    "status": "blocked",
                    "artifacts": [],
                    "notes_md": f"{tid} blocked: simulated obstacle",
                },
            }
        writes = self.writes_by_task.get(tid, [])
        for rel in writes:
            p = Path(cwd) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"# written by {tid}\n")
            # Stage the write so the engine's false-`done` guard
            # (`git status --porcelain` in the worktree) sees the INDIVIDUAL
            # repo-relative path. Untracked NESTED files otherwise collapse to
            # the parent dir (e.g. `scripts/`) in porcelain v1, which would not
            # match the declared write `scripts/foo.py`. A real claude worker's
            # write is detected the same way; staging here makes the FakeCliRunner
            # deterministic against that porcelain quirk.
            subprocess.run(
                ["git", "add", "--", rel],
                cwd=cwd,
                check=False,
                capture_output=True,
            )
        artifacts = [{"path": w, "sha": "deadbeef"} for w in writes] or [
            {"path": f"{tid}.noop", "sha": "deadbeef"}
        ]
        return {
            "usage": {"output_tokens": 5, "input_tokens": 3},
            "total_cost_usd": 0.0,
            "is_error": False,
            "subtype": "success",
            "session_id": "fake-session",
            "num_turns": 1,
            "stop_reason": "end_turn",
            "structured_output": {
                "type": "task_result",
                "task_id": tid,
                "attempt": attempt,
                "status": "done",
                "artifacts": artifacts,
                "notes_md": f"{tid} done",
            },
        }


def _git_init_clone(tmp_path: Path) -> Path:
    clone = tmp_path / "clone"
    clone.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "PATH": __import__("os").environ.get("PATH", ""),
    }
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=clone, env=env, check=True)
    (clone / "seed.txt").write_text("seed")
    subprocess.run(["git", "add", "-A"], cwd=clone, env=env, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=clone, env=env, check=True)
    return clone


@_SKIP_ENGINE
def test_host_cycle_executor_e2e_two_wave_dag(tmp_path):
    """Drive a 2-wave Action-Items DAG through the REAL engine with a
    FakeCliRunner: assert the produced files land in the clone after worktree
    merge and the success outcome dict matches team mode's shape."""
    clone = _git_init_clone(tmp_path)
    items = [
        # Wave 1 — two write-disjoint implementers.
        {
            "id": "AI-1",
            "touches": ["scripts/foo.py"],
            "reads": [],
            "depends_on": [],
            "wave": 1,
            "owner": "backend-engineer-1",
        },
        {
            "id": "AI-2",
            "touches": ["scripts/bar.py"],
            "reads": [],
            "depends_on": [],
            "wave": 1,
            "owner": "backend-engineer-1",
        },
        # Wave 2 — depends on + reads from AI-1's output.
        {
            "id": "AI-3",
            "touches": ["tests/test_foo.py"],
            "reads": ["scripts/foo.py"],
            "depends_on": ["AI-1"],
            "wave": 2,
            "owner": "sdet-1",
        },
    ]
    writes_by_task = {
        "AI-1": ["scripts/foo.py"],
        "AI-2": ["scripts/bar.py"],
        "AI-3": ["tests/test_foo.py"],
    }
    runner = _HonestHostFakeRunner(writes_by_task)

    out = host_cycle_executor(
        action_items=items,
        existing_files=frozenset({"seed.txt"}),
        clone_dir=clone,
        roster=["backend-engineer-1", "sdet-1"],
        subject="test subject",
        runner=runner,
        journal_path=tmp_path / "journal.json",
    )

    # ── outcome dict shape matches team mode's success variant ──────────────
    assert out["status"] == "success"
    assert out["subject"] == "test subject"
    assert "commit_sha" in out  # present (None — commit is the orchestrator's job)
    assert "participants" in out
    assert out["participants"] == ["backend-engineer-1", "sdet-1"]

    # ── the engine was called once per task with correct dispatch identity ───
    dispatched_tids = sorted(runner._tid_attempt(c["argv"])[0] for c in runner.calls)
    assert dispatched_tids == ["AI-1", "AI-2", "AI-3"]

    # ── the FakeCliRunner-produced files landed in the CLONE after merge ─────
    assert (clone / "scripts" / "foo.py").exists()
    assert (clone / "scripts" / "bar.py").exists()
    assert (clone / "tests" / "test_foo.py").exists()


@_SKIP_ENGINE
def test_host_cycle_executor_e2e_tasks_carry_correct_fields(tmp_path):
    """The engine receives tasks with the correct field mapping — verified by
    intercepting the prompt argv the runner records (task_id) and asserting the
    files written reflect the touches→writes mapping + the wave-2 barrier."""
    clone = _git_init_clone(tmp_path)
    items = [
        {
            "id": "AI-1",
            "touches": ["scripts/foo.py"],
            "reads": [],
            "depends_on": [],
            "wave": 1,
            "owner": "backend-engineer-1",
        },
        {
            "id": "AI-3",
            "touches": ["tests/test_foo.py"],
            "reads": ["scripts/foo.py"],
            "depends_on": ["AI-1"],
            "wave": 2,
            "owner": "sdet-1",
        },
    ]
    runner = _HonestHostFakeRunner({"AI-1": ["scripts/foo.py"], "AI-3": ["tests/test_foo.py"]})
    out = host_cycle_executor(
        action_items=items,
        existing_files=frozenset({"seed.txt"}),
        clone_dir=clone,
        roster=["backend-engineer-1", "sdet-1"],
        runner=runner,
        journal_path=tmp_path / "journal.json",
    )
    assert out["status"] == "success"
    # AI-3 (wave 2) must dispatch AFTER AI-1 (wave 1) — the barrier. The runner
    # records calls in completion order; AI-1 must appear before AI-3.
    order = [runner._tid_attempt(c["argv"])[0] for c in runner.calls]
    assert order.index("AI-1") < order.index("AI-3")
    assert (clone / "scripts" / "foo.py").exists()
    assert (clone / "tests" / "test_foo.py").exists()


@_SKIP_ENGINE
def test_host_cycle_executor_e2e_blocked_task_abandons(tmp_path):
    """Drive the REAL engine with a FakeCliRunner where ONE task reports a
    terminal ``status="blocked"`` envelope (and writes no file). This proves the
    production wiring of the "engine ran → a worker reported blocked → cycle
    abandoned" path end-to-end (the pure interpreter test carries the
    branch-mutation coverage).

    Both tasks are INDEPENDENT wave-1 writers (no depends_on), so the blocked
    task has NO dependents and there is no cascade-abandon to muddy the outcome:
    AI-1 succeeds (writes its file + merges), AI-2 returns blocked. The engine
    passes the blocked envelope through to `results` as a non-`done` Mapping
    (single dispatch, no retry — host_scheduler.py:941-946), which
    `_interpret_engine_results` flags via the `status != "done"` branch.
    """
    clone = _git_init_clone(tmp_path)
    items = [
        {
            "id": "AI-1",
            "touches": ["scripts/foo.py"],
            "reads": [],
            "depends_on": [],
            "wave": 1,
            "owner": "backend-engineer-1",
        },
        {
            "id": "AI-2",
            "touches": ["scripts/bar.py"],
            "reads": [],
            "depends_on": [],
            "wave": 1,
            "owner": "backend-engineer-1",
        },
    ]
    runner = _HonestHostFakeRunner(
        {"AI-1": ["scripts/foo.py"], "AI-2": ["scripts/bar.py"]},
        blocked_tasks={"AI-2"},
    )

    out = host_cycle_executor(
        action_items=items,
        existing_files=frozenset({"seed.txt"}),
        clone_dir=clone,
        roster=["backend-engineer-1"],
        subject="test subject",
        runner=runner,
        journal_path=tmp_path / "journal.json",
    )

    # ── the blocked worker → no_consensus abandonment ───────────────────────
    assert out["status"] == "abandoned"
    assert out["reason"] == "no_consensus"
    assert out["phase_reached"] == "implementation"
    # The detail names the blocked task and its non-`done` status.
    assert "AI-2" in out["detail"]
    assert "status=" in out["detail"]
    # AI-1 (the clean done) is NOT named as a failure.
    assert "AI-1:" not in out["detail"]

    # ── both tasks were dispatched; AI-2 wrote NO file (it blocked) ─────────
    dispatched_tids = sorted(runner._tid_attempt(c["argv"])[0] for c in runner.calls)
    assert dispatched_tids == ["AI-1", "AI-2"]
    assert not (clone / "scripts" / "bar.py").exists()
