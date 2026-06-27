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

import importlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.abandonment import VALID_REASONS
from scripts.ci_runner import run_ci_checks
from scripts.dispatch_templates import (
    TEAMMATE_REPLY_RULE,
    phase_4_implementer,
    phase_5b_prime_fix,
    phase_5b_prime_pm_acceptance,
    phase_5b_prime_reviewer,
    phase_5b_prime_reviewer_mesh,
)
from scripts.fix_loop import _CHECK_TO_REASON, Finding
from scripts.host_executor import (
    _REVIEW_TERMINAL_RULE,
    _coalesce_blockers_by_file,
    _collect_review_findings,
    _consolidate_mesh,
    _interpret_engine_results,
    _make_briefing_for,
    _make_escalate_fn,
    _make_fix_briefing_for,
    _make_mesh_briefing_for,
    _make_model_for,
    _make_pm_briefing_for,
    _make_review_briefing_for,
    _parse_mesh_response,
    _ReviewHardAbandon,
    _run_ci_gate,
    build_engine_tasks,
    build_fix_tasks,
    build_mesh_tasks,
    build_pm_task,
    build_review_tasks,
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


# ── live e2e gate (real `claude` CLI + native sandbox) ──────────────────────
# `sandbox_runtime_available` lives in atelier's IN-WINDOW `scripts.cli_dispatch`;
# kaizen cannot import it at module level (its `scripts` resolves to kaizen).
# Probe it once through a transient engine window, defaulting False on any error
# so a fresh box (no atelier / no sandbox) skips cleanly at collection.
_HAS_CLAUDE = shutil.which("claude") is not None
_HAS_SANDBOX = False
if _HAS_ENGINE:
    try:
        from scripts.atelier_engine import assert_engine_available, atelier_engine

        with atelier_engine(assert_engine_available()):
            _HAS_SANDBOX = importlib.import_module(
                "scripts.cli_dispatch"
            ).sandbox_runtime_available()
    except Exception:
        _HAS_SANDBOX = False

_LIVE = pytest.mark.skipif(
    not (_HAS_ENGINE and _HAS_CLAUDE and _HAS_SANDBOX),
    reason="live e2e needs the atelier engine + `claude` on PATH + a native sandbox (bwrap/socat)",
)

# The host CI-mirror gate tests drive the REAL `run_ci_checks`, which shells out
# to `ruff` on a temp clone that opts into ruff. Where ruff isn't on PATH the
# check resolves to `status="skip"` and the pass/fail assertions can't hold —
# skip cleanly rather than fail. CI installs ruff in the Tests job (ci.yml) so
# these RUN there; a dev box without ruff skips them.
_SKIP_NO_RUFF = pytest.mark.skipif(
    shutil.which("ruff") is None,
    reason="`ruff` not on PATH; host CI-gate tests invoke the real run_ci_checks which needs it",
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
        review=False,  # Phase-4-only test; M8a-2b review covered by dedicated tests below
        # M8a-2c: the seed clone has no tests, so a real `pytest` gate is noise
        # for this Phase-4 dispatch/merge test. `true` is a no-op exit-0 gate
        # (the gate's own coverage is in the dedicated CI-gate tests below).
        test_command="true",
    )

    # ── outcome dict shape matches team mode's success variant ──────────────
    assert out["status"] == "success"
    assert out["subject"] == "test subject"
    # M8a-2c: the executor is now self-contained — a clean success COMMITS and
    # stamps a real 40-hex sha + Memex slug (was None pre-M8a-2c).
    assert re.match(r"^[0-9a-f]{40}$", out["commit_sha"]), f"not a real sha: {out['commit_sha']!r}"
    assert out["minutes_memex_slug"] == "kaizen:cycle:host-1"
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
        review=False,  # Phase-4-only test; M8a-2b review covered by dedicated tests below
        test_command="true",  # M8a-2c: no-op CI gate — this is a dispatch/merge test
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


# ════════════════════════════════════════════════════════════════════════════
# M8a-2b — Phase 5b' review-pairing (re-homed Star→Mesh→Star) + fix loop tests.
#
# The unit tests below are PURE (no engine): task-dict builders, the briefing
# closures, the mesh parser, and the C4 severity-gated consolidation. The two
# e2e tests at the bottom drive the REAL engine via a phase-aware FakeCliRunner
# (`@_SKIP_ENGINE`). Several tests (C4 #5-#8, hard-abandon #9-#10, unique-id #11)
# are mutation-proven against `scripts/host_executor.py` — the named mutation was
# applied, the test confirmed RED for the right reason, then REVERTED (the
# evidence is in the agent's final report; the committed tree carries ONLY the
# test additions).
# ════════════════════════════════════════════════════════════════════════════


def _finding(
    fid: str, severity: str, reviewer: str, file_line: str = "scripts/foo.py:1"
) -> Finding:
    """Build a Finding with a one-line prose body (avoids the Layer-B
    multi-line blockquote path so the assertions stay byte-stable)."""
    return Finding(
        finding_id=fid,
        reviewer=reviewer,
        severity=severity,
        finding=f"{severity} issue from {reviewer}",
        file_line=file_line,
    )


# ── #1 — build_review_tasks: broadcast reads-union + read-only ──────────────


def test_build_review_tasks_broadcast_reads_union_and_readonly():
    """USER DECISION 1 (parity): every reviewer task reads the SORTED UNION of
    ALL impl writes (NOT round-robin), is READ-ONLY (writes==[]), depends on EVERY
    impl id, carries NO `reviews` key, phase=='review', and the ids are globally
    unique per reviewer. Mut: round-robin reads (each reviewer sees only one file)
    OR a `reviews` field (would double-dispatch through the engine's own loop)."""
    impl_tasks = [
        {"task_id": "AI-1", "writes": ["scripts/b.py"]},
        {"task_id": "AI-2", "writes": ["scripts/a.py", "scripts/c.py"]},
    ]
    reviewers = ["security-engineer-1", "software-architect-1"]
    tasks = build_review_tasks(reviewers, [], impl_tasks, iter_n=1)

    assert len(tasks) == 2
    union = ["scripts/a.py", "scripts/b.py", "scripts/c.py"]  # SORTED union of all writes
    for t in tasks:
        assert t["writes"] == []  # read-only — no worktree carved, false-done exempt
        assert t["reads"] == union  # broadcast: EVERY reviewer sees the FULL set
        # depends_on EMPTY: single-window design dispatches impl + review as
        # separate pipeline calls, so an in-dispatch dep on the (absent) impl
        # tasks would trip the engine's OrphanDepsError. Ordering is enforced by
        # the sequential dispatch, not an edge.
        assert t["depends_on"] == []
        assert t["phase"] == "review"
        assert "reviews" not in t  # engine's own review_pairing stays empty
        assert t["parallel_group"] == 0
    # ids are globally unique per reviewer.
    assert [t["task_id"] for t in tasks] == ["R1-0", "R1-1"]
    assert len({t["task_id"] for t in tasks}) == 2


# ── #1b — build_mesh_tasks: round-2 reads = impl WRITES, never finding refs ──


def test_build_mesh_tasks_reads_are_impl_writes_not_finding_refs():
    """REGRESSION (M8b live e2e crash): round-2 (Mesh) `reads` MUST be the SORTED
    UNION of impl WRITES — parity with round-1 `build_review_tasks` — NOT derived
    from round-1 reviewers' FINDING file-references.

    A reviewer can flag a NON-EXISTENT file (e.g. a SUGGESTED test file). If that
    finding's file-ref fed the mesh task's `reads`, the engine's reads-satisfiable
    gate (validate_dag gate 3) rejects the DAG with UnsatisfiableReadsError → an
    uncaught crash. The impl writes (the actual merged change set) ARE satisfiable
    because the mesh dispatch passes the augmented `review_existing` (base set plus
    impl writes). The peer findings each mesh reviewer cross-checks ride via the BRIEFING
    CLOSURE, NOT via `reads`.

    Mut (the shipped bug): deriving `reads` from finding file-refs would put the
    non-existent 'tests/test_divide.py' into `reads` and OMIT the impl writes."""
    impl_tasks = [
        {"task_id": "AI-1", "writes": ["mathlib/divide.py", "mathlib/__init__.py"]},
    ]
    # A round-1 reviewer referenced a NON-EXISTENT suggested test file.
    r1_findings_by_reviewer = {
        "security-engineer-1": [
            _finding("R1-0-1", "blocker", "security-engineer-1", "tests/test_divide.py:1"),
        ],
        "software-architect-1": [
            _finding("R1-1-1", "major", "software-architect-1", "mathlib/divide.py:42"),
        ],
    }
    reviewers = ["security-engineer-1", "software-architect-1"]
    tasks = build_mesh_tasks(reviewers, [], r1_findings_by_reviewer, impl_tasks, iter_n=1)

    expected_reads = ["mathlib/__init__.py", "mathlib/divide.py"]  # SORTED impl writes
    assert len(tasks) == 2
    for t in tasks:
        assert t["reads"] == expected_reads, (
            f"mesh reads must be the sorted impl writes, got {t['reads']!r}"
        )
        # The non-existent suggested test file MUST NOT appear in reads (the bug).
        assert "tests/test_divide.py" not in t["reads"]
        # mathlib/divide.py:42's file is an impl write, so it IS in reads — but
        # only because it is an impl WRITE, not because a finding referenced it.
        assert t["writes"] == []
        assert t["phase"] == "review"
        assert "reviews" not in t
    assert [t["task_id"] for t in tasks] == ["M1-0", "M1-1"]


def test_build_mesh_tasks_mesh_dag_passes_validate_dag_with_augmented_existing():
    """REGRESSION (focused): the DAG built from `build_mesh_tasks` for a scenario
    where a round-1 reviewer flagged a NON-EXISTENT file PASSES the engine's
    `validate_dag` reads-satisfiable gate when validated against the AUGMENTED
    existing-file set (base set plus impl writes) — exactly the set the mesh dispatch
    passes as `round_existing=review_existing`. Before the fix the mesh `reads`
    held 'tests/test_divide.py' (not in existing, not produced) → gate 3 fails."""
    from scripts.dag import UnsatisfiableReadsError, validate_dag

    base_existing = {"seed.txt"}
    impl_writes = ["mathlib/divide.py", "mathlib/__init__.py"]
    impl_tasks = [{"task_id": "AI-1", "writes": impl_writes}]
    r1_findings_by_reviewer = {
        "security-engineer-1": [
            _finding("R1-0-1", "blocker", "security-engineer-1", "tests/test_divide.py:1"),
        ],
        "software-architect-1": [
            _finding("R1-1-1", "major", "software-architect-1", "mathlib/divide.py:42"),
        ],
    }
    reviewers = ["security-engineer-1", "software-architect-1"]
    mesh_tasks = build_mesh_tasks(reviewers, [], r1_findings_by_reviewer, impl_tasks, iter_n=1)

    # Augmented existing set the mesh dispatch uses (host_executor.py:1680).
    review_existing = frozenset(set(base_existing) | set(impl_writes))
    # Convert the engine-task dicts to validate_dag item shape (single wave —
    # read-only mesh tasks have no inter-edges).
    items = [
        {
            "id": t["task_id"],
            "touches": list(t["writes"]),
            "reads": list(t["reads"]),
            "depends_on": list(t["depends_on"]),
            "wave": 1,
        }
        for t in mesh_tasks
    ]
    v = validate_dag(items, existing_files=review_existing)
    assert v.ok, f"mesh DAG must pass validate_dag, errors={v.errors!r}"
    assert not any(isinstance(e, UnsatisfiableReadsError) for e in v.errors)


# ── #2 — no `reviews` field ⇒ engine stays single-dispatch (pure proxy) ─────


def test_no_reviews_field_keeps_pipeline_single_dispatch():
    """The review/mesh/PM/fix builders emit NO `reviews` key, so the engine's
    `build_review_pairing` derives an EMPTY pairing and never enters its own
    (BLIND-redispatch) review loop — kaizen owns the loop. Asserting the absence
    of the key on EVERY builder is the pure proxy for "single dispatch per round".
    Mut: a `reviews` key on any builder would make the engine double-dispatch."""
    impl_tasks = [{"task_id": "AI-1", "writes": ["scripts/foo.py"]}]
    r = build_review_tasks(["security-engineer-1"], [], impl_tasks, iter_n=1)
    m = build_mesh_tasks(
        ["security-engineer-1"], [], {"security-engineer-1": []}, impl_tasks, iter_n=1
    )
    f = build_fix_tasks(
        {"scripts/foo.py": [_finding("R1-0-1", "blocker", "security-engineer-1")]},
        {"scripts/foo.py": "backend-engineer-1"},
        "pm-1",
        iter_n=1,
    )
    pm = build_pm_task([_finding("R1-0-1", "blocker", "security-engineer-1")], "pm-1", iter_n=1)
    for t in (*r, *m, *f, pm):
        assert "reviews" not in t, f"task {t['task_id']} leaked a `reviews` key"


# ── #3 — review task read-only ⇒ runs in clone, not a worktree ──────────────


@_SKIP_ENGINE
def test_review_task_readonly_runs_in_clone_not_worktree(tmp_path):
    """Drive ONE review task (writes==[]) through the REAL engine: its runner cwd
    is the SHARED base clone (NO worktree carved — C1), and a `done` envelope with
    a noop artifact + empty declared writes is NOT converted to FAILED_ATTEMPT (the
    false-`done` guard is exempt for non-writers). Mut: declaring `writes` would
    carve a worktree (cwd != clone) AND trip the false-done guard."""
    clone = _git_init_clone(tmp_path)
    runner = _PhaseAwareHostFakeRunner(notes_by_task={"R1-0": "NO ISSUES"})
    results = _drive_single_review_round(
        clone,
        tmp_path,
        tasks=[
            {
                "task_id": "R1-0",
                "parallel_group": 0,
                "depends_on": [],  # no impl deps — keep this isolated probe orphan-free
                "writes": [],
                "reads": [],
                "assigned_persona": "security-engineer-1",
                "phase": "review",
            }
        ],
        runner=runner,
    )
    # The review worker ran in the base clone, NOT an isolated worktree.
    assert len(runner.calls) == 1
    assert Path(runner.calls[0]["cwd"]).resolve() == clone.resolve()
    # The done+empty-writes envelope survived (not a failed-attempt sentinel).
    assert isinstance(results[0], dict)
    assert results[0]["status"] == "done"
    # notes_md is TOP-LEVEL on the returned envelope (R2).
    assert results[0]["notes_md"] == "NO ISSUES"


# ── #4 — mesh round injects PEER findings (not self) into each brief ────────


def test_mesh_round_redispatches_with_peer_findings_injected():
    """Round-2 mesh brief for reviewer A (M1-0) contains B's round-1 finding
    (blockquoted/bulleted by the template), NOT A's own. Mut: peer findings omitted
    (brief empty) OR self-leak (A sees A's own finding)."""
    a_finding = _finding("R1-0-1", "major", "security-engineer-1", "scripts/a.py:10")
    b_finding = _finding("R1-1-1", "minor", "software-architect-1", "scripts/b.py:20")
    # Caller computes the exclusion: M1-0 (reviewer A) sees only B's finding.
    peer_map = {"M1-0": [b_finding], "M1-1": [a_finding]}
    items = [{"id": "AI-1", "touches": ["scripts/a.py"]}]
    briefing = _make_mesh_briefing_for(
        {"AI-1": items[0]},
        items,
        peer_map,
        phase_5b_prime_reviewer_mesh,
        TEAMMATE_REPLY_RULE,
    )
    brief_a = briefing({"task_id": "M1-0"}, 1)
    # A sees B's finding id + prose; NOT A's own.
    assert "R1-1-1" in brief_a
    assert "minor issue from software-architect-1" in brief_a
    assert "R1-0-1" not in brief_a  # no self-leak
    assert "major issue from security-engineer-1" not in brief_a
    # Symmetric: B's brief sees A's finding, not B's own.
    brief_b = briefing({"task_id": "M1-1"}, 1)
    assert "R1-0-1" in brief_b
    assert "R1-1-1" not in brief_b


# ── #5 — C4: drop unconfirmed minor, RETAIN unconfirmed blocker (+flag) ─────


def test_consolidation_drops_unconfirmed_minor_keeps_unconfirmed_blocker():
    """C4 severity-gated weeding: an unconfirmed `minor` is DROPPED; an unconfirmed
    `blocker` is RETAINED and flagged peer_unconfirmed=True. Mut: applying the
    ≥1-peer rule to blockers (F9 collapse — would drop the unconfirmed blocker) OR
    keeping every minor verbatim (would keep the unconfirmed minor)."""
    blocker = _finding("R1-0-1", "blocker", "security-engineer-1", "scripts/a.py:1")
    minor = _finding("R1-0-2", "minor", "security-engineer-1", "scripts/b.py:1")
    # Two reviewers, NEITHER confirms anything (empty verdicts each).
    survivors, peer_unconfirmed = _consolidate_mesh(
        [blocker, minor],
        {"security-engineer-1": {}, "software-architect-1": {}},
        [],
        n_reviewers=2,
    )
    surviving_ids = {f.finding_id for f in survivors}
    assert "R1-0-1" in surviving_ids  # unconfirmed blocker RETAINED
    assert "R1-0-2" not in surviving_ids  # unconfirmed minor DROPPED
    assert peer_unconfirmed.get("R1-0-1") is True  # blocker flagged for the PM gate
    assert "R1-0-2" not in peer_unconfirmed


# ── #6 — C4: self-retract drops ANY severity ────────────────────────────────


def test_consolidation_self_retract_drops_any_severity():
    """A RETRACT verdict on a finding drops it regardless of severity — even a
    blocker. Mut: ignore self-retract (the retracted blocker would survive)."""
    blocker = _finding("R1-0-1", "blocker", "security-engineer-1")
    survivors, peer_unconfirmed = _consolidate_mesh(
        [blocker],
        {"security-engineer-1": {"R1-0-1": "RETRACT"}, "software-architect-1": {}},
        [],
        n_reviewers=2,
    )
    assert survivors == []  # retracted blocker dropped
    assert peer_unconfirmed == {}  # nothing retained → nothing flagged


# ── #7 — C4: escalation raises severity (never demotes) ─────────────────────


def test_consolidation_escalation_raises_severity():
    """A peer ESCALATE raises a `minor` to `major` (now blocking). The escalate
    also counts as a confirm, so the raised finding survives the minor/nit weed
    AND blocks. Mut: escalate ignored (minor weeded out) OR demote allowed (a
    lower-severity escalate target would lower a blocker)."""
    minor = _finding("R1-0-1", "minor", "security-engineer-1", "scripts/a.py:1")
    survivors, _peer = _consolidate_mesh(
        [minor],
        {"security-engineer-1": {}, "software-architect-1": {"R1-0-1": ("ESCALATE", "major")}},
        [],
        n_reviewers=2,
    )
    assert len(survivors) == 1
    assert survivors[0].finding_id == "R1-0-1"
    assert survivors[0].severity == "major"  # raised from minor
    # Non-vacuity / no-demote anchor: a LOWER escalate target never demotes a blocker.
    blocker = _finding("R1-0-9", "blocker", "security-engineer-1", "scripts/z.py:1")
    surv2, _ = _consolidate_mesh(
        [blocker],
        {"security-engineer-1": {}, "software-architect-1": {"R1-0-9": ("ESCALATE", "minor")}},
        [],
        n_reviewers=2,
    )
    assert surv2[0].severity == "blocker"  # never demoted to minor


# ── #8 — single-reviewer vacuous quorum ─────────────────────────────────────


def test_single_reviewer_vacuous_quorum():
    """n_reviewers==1: no peers ⇒ no weeding ⇒ EVERY non-self-retracted finding
    survives at its ORIGINAL severity (even a minor — the peer-confirm gate is a
    multi-reviewer rule). Mut: applying the ≥1-peer rule unconditionally would
    empty the set even though the sole reviewer DID flag the issue."""
    blocker = _finding("R1-0-1", "blocker", "security-engineer-1")
    minor = _finding("R1-0-2", "minor", "security-engineer-1", "scripts/b.py:1")
    survivors, peer_unconfirmed = _consolidate_mesh([blocker, minor], {}, [], n_reviewers=1)
    surviving_ids = {f.finding_id for f in survivors}
    assert surviving_ids == {"R1-0-1", "R1-0-2"}  # BOTH survive (minor not weeded)
    assert peer_unconfirmed == {}  # vacuous quorum sets no flags


# ── #9 — silent/failed round-1 reviewer ⇒ HARD abandon ──────────────────────


def test_silent_reviewer_is_hard_abandon():
    """A round-1 reviewer whose envelope is a failed-attempt sentinel OR a
    non-`done` status is a SILENT reviewer — `_collect_review_findings` raises
    `_ReviewHardAbandon` (the loop turns it into review_unrecoverable). Mut:
    soft-skip would ship an UNREVIEWED change."""
    reviewers = ["security-engineer-1", "software-architect-1"]
    sentinel = object()

    def is_failed_attempt(v):
        return v is sentinel

    # Sub-branch A — failed-attempt sentinel.
    results_a = [
        {"type": "task_result", "task_id": "R1-0", "status": "done", "notes_md": "NO ISSUES"},
        sentinel,
    ]
    with pytest.raises(_ReviewHardAbandon, match="failed-attempt sentinel"):
        _collect_review_findings(
            reviewers, results_a, iter_n=1, is_failed_attempt=is_failed_attempt
        )

    # Sub-branch B — a VALID terminal envelope with a non-`done` status.
    results_b = [
        {"type": "task_result", "task_id": "R1-0", "status": "done", "notes_md": "NO ISSUES"},
        {"type": "task_result", "task_id": "R1-1", "status": "blocked", "notes_md": "stuck"},
    ]
    with pytest.raises(_ReviewHardAbandon, match="expected 'done'"):
        _collect_review_findings(
            reviewers, results_b, iter_n=1, is_failed_attempt=is_failed_attempt
        )


# ── #10 — malformed / zero-verdict mesh reply ⇒ HARD abandon ────────────────


def test_malformed_mesh_reply_is_hard_abandon():
    """A mesh reply that parses to ZERO recognized verdict lines AND zero finding
    lines is MALFORMED — the loop HARD-abandons it (C4 strict posture). The pure
    parser surfaces this as an empty (verdicts, net_new); the loop's emptiness
    check raises. Mut: silently treating a zero-signal reply as 'all unconfirmed'
    would let an unreviewed change ship."""
    # A reply full of prose but NO verdict/finding grammar.
    verdicts, net_new = _parse_mesh_response(
        "I looked at the diff and it all seems fine to me, nothing to report.",
        "security-engineer-1",
        prefix="R1-mesh-0",
    )
    assert verdicts == {}
    assert net_new == []  # the combined emptiness is what the loop HARD-abandons on

    # Non-vacuity anchor: a recognized verdict line is NOT treated as malformed.
    verdicts2, _net2 = _parse_mesh_response(
        "CONFIRM R1-1-1", "security-engineer-1", prefix="R1-mesh-0"
    )
    assert verdicts2 == {"R1-1-1": "CONFIRM"}

    # An ESCALATE without a severity token is IGNORED (not a silent demote) — but
    # is also not, by itself, a recognized verdict (so a reply of only that line
    # would be malformed). An ESCALATE WITH a severity IS recognized.
    v_no_sev, _ = _parse_mesh_response("ESCALATE R1-1-1", "r", prefix="R1-mesh-0")
    assert v_no_sev == {}
    v_sev, _ = _parse_mesh_response("ESCALATE R1-1-1 major", "r", prefix="R1-mesh-0")
    assert v_sev == {"R1-1-1": ("ESCALATE", "major")}


# ── #11 — globally-unique finding ids across reviewers ──────────────────────


def test_globally_unique_finding_ids_across_reviewers():
    """Two reviewers' FIRST findings re-stamp to DISTINCT ids (R1-0-1 vs R1-1-1),
    so an attribution map keys both. Mut: a per-reviewer `R{iter}-{k}` scheme would
    mint `R1-1` for BOTH first findings → a collision that drops one in the
    attribution map (a real F9-attribution loss)."""
    reviewers = ["security-engineer-1", "software-architect-1"]
    results = [
        {
            "type": "task_result",
            "task_id": "R1-0",
            "status": "done",
            "notes_md": "[blocker] scripts/a.py:1 — A's first finding",
        },
        {
            "type": "task_result",
            "task_id": "R1-1",
            "status": "done",
            "notes_md": "[major] scripts/b.py:2 — B's first finding",
        },
    ]
    findings = _collect_review_findings(
        reviewers, results, iter_n=1, is_failed_attempt=lambda _v: False
    )
    ids = [f.finding_id for f in findings]
    assert ids == ["R1-0-1", "R1-1-1"]  # globally unique, NOT a colliding R1-1/R1-1
    # Both survive an attribution map (no collision dropping one).
    attribution = {f.finding_id: f.reviewer for f in findings}
    assert attribution == {
        "R1-0-1": "security-engineer-1",
        "R1-1-1": "software-architect-1",
    }


# ── #12 — PM acceptance literal-prefix gate ─────────────────────────────────


def test_pm_acceptance_literal_prefix_exits_loop():
    """PM-acceptance is the verbatim team_executor.py:2381 gate: a reply whose
    stripped+uppercased text STARTS WITH `ACCEPT` is a clean accept; everything
    else (incl. an `ABANDON:` prefix) is a REJECT. Mut: a relaxed prefix (e.g.
    substring `accept` anywhere, or treating `ABANDON` as accept)."""

    def pm_accepts(resp: str) -> bool:
        # The exact production expression (host_executor.py:1085).
        return (resp or "").strip().upper().startswith("ACCEPT")

    assert pm_accepts("ACCEPT — out of scope for this cycle") is True
    assert pm_accepts("  accept, these are fine  ") is True  # strip + upper
    assert pm_accepts("ABANDON: cannot proceed") is False  # NOT an accept
    assert pm_accepts("REJECT — keep fixing") is False
    assert pm_accepts("We should accept these eventually") is False  # not a PREFIX
    assert pm_accepts("") is False
    assert pm_accepts(None) is False


# ── #13 — fix routes to the file's Phase-4 owner + coalesces same-file ──────


def test_fix_routes_to_implementer_not_reviewer_and_coalesces():
    """Two blockers on the SAME file → ONE fix task routed to that file's Phase-4
    OWNER (never the reviewer who flagged it). Mut: route to the reviewer
    (assigned_persona == reviewer) OR one-fix-per-finding (two tasks)."""
    f1 = _finding("R1-0-1", "blocker", "security-engineer-1", "scripts/foo.py:1")
    f2 = _finding("R1-1-1", "major", "software-architect-1", "scripts/foo.py:99")
    coalesced = {"scripts/foo.py": [f1, f2]}  # same file → coalesced
    tasks = build_fix_tasks(
        coalesced,
        {"scripts/foo.py": "backend-engineer-1"},  # the Phase-4 owner
        "pm-1",
        iter_n=2,
    )
    assert len(tasks) == 1  # ONE task for the file (coalesced), not two
    t = tasks[0]
    assert t["assigned_persona"] == "backend-engineer-1"  # the OWNER, not a reviewer
    assert t["assigned_persona"] not in ("security-engineer-1", "software-architect-1")
    assert t["writes"] == ["scripts/foo.py"]  # WRITER task
    assert t["task_id"] == "FIX2-0"
    assert t["phase"] == "fix"
    # An UNOWNED file falls back to the PM (the reused _find_owner_for_finding).
    orphan = _finding("R1-0-2", "blocker", "security-engineer-1", "scripts/unowned.py:5")
    t2 = build_fix_tasks({"scripts/unowned.py": [orphan]}, {}, "pm-1", iter_n=2)[0]
    assert t2["assigned_persona"] == "pm-1"


# ── #13b — M8b Bug#4: directory/non-file finding targets never reach `writes` ─


def test_coalesce_separates_directory_targets_from_file_targets():
    """M8b Bug#4 regression. A reviewer blocker whose `file_line` points at a
    DIRECTORY (e.g. `tests/` or `tests/:3`) or is empty/non-file must NOT be
    grouped as a fix-writer file target (a directory in `writes` is malformed for
    the engine's write-disjointness gate + worktree carving → merge conflict /
    false-`done` reject → the whole cycle abandons). It MUST be returned as a
    NON-ROUTABLE finding (never silently dropped — that would falsely converge the
    review loop). A normal `file.py:42` target still routes.

    Pre-fix bug: `_coalesce_blockers_by_file` returned `{"tests/": [...]}` and
    `build_fix_tasks` then emitted `writes=["tests/"]`."""
    good = _finding("R1-0-1", "blocker", "security-engineer-1", "scripts/foo.py:42")
    dir_trailing = _finding("R1-0-2", "blocker", "security-engineer-1", "tests/")
    dir_lined = _finding("R1-1-1", "major", "software-architect-1", "tests/:3")
    empty = _finding("R1-1-2", "blocker", "software-architect-1", "")

    by_file, non_routable = _coalesce_blockers_by_file([good, dir_trailing, dir_lined, empty])

    # Only the real file target is a fix-writer group.
    assert set(by_file) == {"scripts/foo.py"}
    assert by_file["scripts/foo.py"] == [good]

    # The three non-file targets are surfaced, not dropped.
    non_routable_ids = {f.finding_id for f in non_routable}
    assert non_routable_ids == {"R1-0-2", "R1-1-1", "R1-1-2"}


def test_build_fix_tasks_never_emits_directory_in_writes():
    """`build_fix_tasks` is a defensive backstop: a directory target must never
    reach the engine as a `writes` entry. Feeding a directory raises fail-loud
    rather than carving a malformed worktree."""
    blocker = _finding("R1-0-1", "blocker", "security-engineer-1", "tests/")
    with pytest.raises(ValueError, match=r"directory|non-file|writes"):
        build_fix_tasks(
            {"tests/": [blocker]},
            {},
            "pm-1",
            iter_n=1,
        )


def test_coalesce_normal_file_targets_unaffected():
    """No regression for the normal path: a `path/to/file.py:42` finding routes to
    its file group and produces NO non-routable findings."""
    f1 = _finding("R1-0-1", "blocker", "security-engineer-1", "scripts/a.py:42")
    f2 = _finding("R1-1-1", "major", "software-architect-1", "scripts/b.py:7")
    by_file, non_routable = _coalesce_blockers_by_file([f1, f2])
    assert set(by_file) == {"scripts/a.py", "scripts/b.py"}
    assert non_routable == []
    tasks = build_fix_tasks(by_file, {}, "pm-1", iter_n=1)
    for t in tasks:
        for w in t["writes"]:
            assert not w.endswith("/") and w, f"directory leaked into writes: {w!r}"


def test_coalesce_fs_guard_catches_directory_without_trailing_slash(tmp_path):
    """M8b Bug#4 hardening: a blocker targeting a directory written WITHOUT a
    trailing slash (`tests`, not `tests/`) is invisible to the string guard but is
    caught by the filesystem guard when `clone_dir` is supplied — the production
    loop always supplies it. A path that does not yet exist (a NEW file the fix
    creates) stays routable."""
    (tmp_path / "tests").mkdir()
    dir_no_slash = _finding("R1-0-1", "blocker", "security-engineer-1", "tests")
    dir_no_slash_lined = _finding("R1-0-2", "major", "software-architect-1", "tests:9")
    real_file = _finding("R1-1-1", "blocker", "security-engineer-1", "scripts/foo.py:42")
    new_file = _finding("R1-1-2", "blocker", "software-architect-1", "tests/new_test.py:1")

    by_file, non_routable = _coalesce_blockers_by_file(
        [dir_no_slash, dir_no_slash_lined, real_file, new_file], tmp_path
    )

    # The existing directory (both spellings) is non-routable; the real file and a
    # not-yet-existing new file under tests/ both route.
    assert set(by_file) == {"scripts/foo.py", "tests/new_test.py"}
    assert {f.finding_id for f in non_routable} == {"R1-0-1", "R1-0-2"}

    # Without clone_dir the no-slash directory slips past the string-only baseline
    # (documents the two-layer contract).
    by_file_str, non_str = _coalesce_blockers_by_file([dir_no_slash])
    assert set(by_file_str) == {"tests"} and non_str == []


def test_coalesce_mixed_routable_and_non_routable_surfaces_both(tmp_path):
    """A round with BOTH a routable blocker and a non-routable one must route the
    fixable file AND surface the non-routable finding (never let one mask the
    other). build_fix_tasks then emits a writer ONLY for the routable file, with no
    directory in any `writes`."""
    (tmp_path / "tests").mkdir()
    routable = _finding("R1-0-1", "blocker", "security-engineer-1", "scripts/a.py:5")
    non_routable_dir = _finding("R1-1-1", "blocker", "software-architect-1", "tests/")

    by_file, non_routable = _coalesce_blockers_by_file([routable, non_routable_dir], tmp_path)
    assert set(by_file) == {"scripts/a.py"}
    assert [f.finding_id for f in non_routable] == ["R1-1-1"]

    tasks = build_fix_tasks(by_file, {}, "pm-1", iter_n=1)
    assert len(tasks) == 1
    assert tasks[0]["writes"] == ["scripts/a.py"]


# ── #14 — iter-2 round-1 brief carries iter-1 survivors as prior_findings ───


def test_mesh_iter2_carries_prior_survivors_forward():
    """The round-1 reviewer briefing factory closes over `prior_findings`; the
    iter-2 closure (built with iter-1 survivors) renders those survivors into the
    incremental-review brief. Mut: dropping the carry-forward (the iter-1 survivor
    would be absent from the iter-2 brief)."""
    prior = [_finding("R1-0-1", "blocker", "security-engineer-1", "scripts/foo.py:1")]
    items = [{"id": "AI-1", "touches": ["scripts/foo.py"]}]
    # iter-2 closure carries the iter-1 survivor.
    briefing2 = _make_review_briefing_for(
        {"AI-1": items[0]},
        items,
        phase_5b_prime_reviewer,
        prior,
        TEAMMATE_REPLY_RULE,
    )
    brief = briefing2({"task_id": "R2-0"}, 1)
    assert "R1-0-1" in brief  # the carried-forward survivor id is in the iter-2 brief
    assert "blocker issue from security-engineer-1" in brief
    # Control: a closure built with NO prior findings does NOT render it.
    briefing_none = _make_review_briefing_for(
        {"AI-1": items[0]}, items, phase_5b_prime_reviewer, None, TEAMMATE_REPLY_RULE
    )
    brief_none = briefing_none({"task_id": "R1-0"}, 1)
    assert "R1-0-1" not in brief_none


# ── closure coverage: PM + fix briefings strip F7 and render the finding ────


def test_pm_and_fix_briefing_closures_strip_f7_and_render():
    """The PM and fix briefing closures render the canonical templates, STRIP the
    F7/reply-format comms trailer (host workers have no SendMessage), and
    append the host terminal rule. Extends the closure-coverage contract to the
    mesh/PM/fix factories (spec §156). Mut: leaving the F7 trailer would instruct a
    host worker to use a primitive it does not have."""
    blocker = _finding("R1-0-1", "blocker", "security-engineer-1", "scripts/foo.py:7")

    pm_briefing = _make_pm_briefing_for(
        {"PM1": [blocker]}, phase_5b_prime_pm_acceptance, TEAMMATE_REPLY_RULE
    )
    pm_body = pm_briefing({"task_id": "PM1"}, 1)
    assert "PM acceptance" in pm_body  # the PM template body survives
    assert "R1-0-1" in pm_body  # the blocker is rendered for the PM
    assert 'SendMessage(to="team-lead"' not in pm_body  # F7 stripped
    assert "terminal task_result envelope" in pm_body  # host terminal rule appended

    fix_briefing = _make_fix_briefing_for(
        {"FIX1-0": blocker}, phase_5b_prime_fix, TEAMMATE_REPLY_RULE
    )
    fix_body = fix_briefing({"task_id": "FIX1-0"}, 1)
    assert "scripts/foo.py:7" in fix_body  # the finding the fix addresses
    assert 'SendMessage(to="team-lead"' not in fix_body  # F7 stripped
    assert "terminal task_result envelope" in fix_body  # host WRITER terminal rule


@_SKIP_ENGINE
def test_pm_fix_closures_work_inside_engine_window():
    """The PM/fix/mesh briefing closures render correctly AFTER entering a real
    atelier_engine() window (where `scripts` resolves to atelier), proving they
    reference pre-bound kaizen objects, not an in-window `scripts.*` import."""
    from scripts.atelier_engine import assert_engine_available, atelier_engine

    blocker = _finding("R1-0-1", "blocker", "security-engineer-1", "scripts/foo.py:7")
    items = [{"id": "AI-1", "touches": ["scripts/foo.py"]}]
    pm_b = _make_pm_briefing_for(
        {"PM1": [blocker]}, phase_5b_prime_pm_acceptance, TEAMMATE_REPLY_RULE
    )
    fix_b = _make_fix_briefing_for({"FIX1-0": blocker}, phase_5b_prime_fix, TEAMMATE_REPLY_RULE)
    mesh_b = _make_mesh_briefing_for(
        {"AI-1": items[0]},
        items,
        {"M1-0": [blocker]},
        phase_5b_prime_reviewer_mesh,
        TEAMMATE_REPLY_RULE,
    )

    root = assert_engine_available()
    with atelier_engine(root):
        pm_body = pm_b({"task_id": "PM1"}, 1)
        fix_body = fix_b({"task_id": "FIX1-0"}, 1)
        mesh_body = mesh_b({"task_id": "M1-0"}, 1)

    assert "R1-0-1" in pm_body
    assert "scripts/foo.py:7" in fix_body
    assert "R1-0-1" in mesh_body


# ── #15 — model_for per-phase + fail-loud on unknown ────────────────────────


def test_model_for_per_phase_and_fail_loud():
    """The per-phase model policy maps implementation/review/fix → opus and RAISES
    ValueError on an unknown phase (a wiring bug must surface at dispatch, not
    degrade silently). Mut: a constant return (every phase → opus, swallowing the
    unknown) OR a swallowed default."""
    mf = _make_model_for()
    assert mf({"phase": "implementation"}, 1) == "opus"
    assert mf({"phase": "review"}, 1) == "opus"
    assert mf({"phase": "fix"}, 1) == "opus"
    with pytest.raises(ValueError, match="no model policy for phase"):
        mf({"phase": "deploy"}, 1)


# ── #16 — mesh skipped when round-1 finds nothing ───────────────────────────


def test_mesh_skipped_when_round1_empty():
    """When round 1 surfaces ZERO findings, consolidation with an empty round-1
    set returns no survivors (the loop's fast path then skips the mesh dispatch
    entirely and exits clean). Mut: dispatching the mesh on an empty set (wasted
    round) or fabricating survivors. The orchestration-level no-dispatch is
    asserted end-to-end in #17's clean-exit path; here the pure consolidation
    over an empty round-1 is the unit anchor."""
    survivors, peer_unconfirmed = _consolidate_mesh([], {}, [], n_reviewers=2)
    assert survivors == []
    assert peer_unconfirmed == {}


# ── #17 / #18 — phase-aware FakeCliRunner + e2e ─────────────────────────────


class _PhaseAwareHostFakeRunner:
    """Phase-aware honest fake of atelier's CLI runner for the Phase-5b' e2e.

    Extends the `_HonestHostFakeRunner` contract by branching on the dispatched
    `task_id` PREFIX:

      * ``R*`` / ``M*`` (round-1 review / round-2 mesh) — READ-ONLY: writes
        NOTHING, emits ``status="done"`` with a single NOOP artifact (a `done`
        envelope MUST carry a non-empty `artifacts` list — empty is rejected by
        ``validate_envelope`` for `done`) and the verdict/finding prose in the
        TOP-LEVEL ``notes_md`` (per-task, looked up in ``notes_by_task``).
      * ``PM*`` (PM-acceptance) — READ-ONLY: emits ``done`` + noop artifact, with
        ``notes_md`` starting ``ACCEPT`` / ``REJECT`` (looked up in
        ``pm_by_task``, default ``REJECT``).
      * ``FIX*`` (fix-round writer) — WRITER: writes + ``git add``s the file the
        task declares (so the engine's false-`done` guard is satisfied), emits a
        ``done`` envelope with one artifact per write.
      * anything else (impl ``AI-*``) — WRITER: same as the existing
        `_HonestHostFakeRunner` write path.

    Records every ``(argv, cwd)`` so the e2e can assert the reviewer ran in the
    SHARED base clone (C1) and never an isolated worktree.
    """

    no_real_process = True
    is_fake = True

    def __init__(
        self,
        impl_writes: dict[str, list[str]] | None = None,
        notes_by_task: dict[str, str] | None = None,
        pm_by_task: dict[str, str] | None = None,
        fix_writes: dict[str, list[str]] | None = None,
        notes_by_prefix: dict[str, str] | None = None,
        pm_default: str = "REJECT — keep fixing",
    ):
        self.impl_writes = impl_writes or {}
        self.notes_by_task = notes_by_task or {}
        # `notes_by_prefix` keys on the task-id prefix WITHOUT the trailing index
        # (e.g. "R1" matches R1-0, R1-1; "M2" matches M2-0, ...) so a whole round's
        # reviewers can share one canned reply without enumerating each idx.
        self.notes_by_prefix = notes_by_prefix or {}
        self.pm_by_task = pm_by_task or {}
        self.fix_writes = fix_writes or {}
        self.pm_default = pm_default
        self.calls: list[dict] = []
        # Optional sync hook(argv, cwd) invoked at the START of every __call__.
        # A regular attribute (NOT a dunder), so per-instance assignment works —
        # `runner.__call__ = fn` would NOT, since `runner(...)` resolves __call__
        # on the TYPE, bypassing the instance attribute.
        self.pre_call_hook = None

    @staticmethod
    def _tid_attempt(argv) -> tuple[str, int]:
        m = re.search(r"Perform task (\S+) \(attempt (\d+)\)", argv[2])
        assert m is not None, f"unexpected prompt argv: {argv!r}"
        return m.group(1), int(m.group(2))

    def _round_prefix(self, tid: str) -> str:
        # "R1-0" -> "R1"; "M2-1" -> "M2"; "PM3" -> "PM3"; "FIX1-0" -> "FIX1".
        return tid.rsplit("-", 1)[0] if "-" in tid else tid

    def _envelope(self, tid: str, attempt: int, status: str, artifacts: list, notes: str) -> dict:
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
                "status": status,
                "artifacts": artifacts,
                "notes_md": notes,
            },
        }

    def _write_files(self, cwd, writes, tid):
        for rel in writes:
            p = Path(cwd) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            # tid-specific content: a FIX rewriting the file an impl task wrote
            # must be a REAL change — identical content → no git diff → the
            # engine's false-`done` guard rejects it.
            p.write_text(f"# written by {tid}\n")
            subprocess.run(["git", "add", "--", rel], cwd=cwd, check=False, capture_output=True)

    async def __call__(self, argv, cwd):
        self.calls.append({"argv": list(argv), "cwd": cwd})
        if self.pre_call_hook is not None:
            self.pre_call_hook(argv, cwd)
        tid, attempt = self._tid_attempt(argv)
        # NOOP artifact for read-only (review/mesh/PM) tasks — a `done` envelope
        # MUST carry a non-empty artifacts list (validate_envelope), and a
        # read-only task is false-done-exempt, so a noop is the honest analog of a
        # real reviewer's no-write `done`.
        noop = [{"path": f"{tid}.noop", "sha": "deadbeef"}]
        if tid.startswith("PM"):
            notes = self.pm_by_task.get(tid, self.pm_default)
            return self._envelope(tid, attempt, "done", noop, notes)
        if tid.startswith(("R", "M")):
            notes = self.notes_by_task.get(tid)
            if notes is None:
                notes = self.notes_by_prefix.get(self._round_prefix(tid), "NO ISSUES")
            return self._envelope(tid, attempt, "done", noop, notes)
        if tid.startswith("FIX"):
            writes = self.fix_writes.get(tid, [])
            self._write_files(cwd, writes, tid)
            arts = [{"path": w, "sha": "deadbeef"} for w in writes] or noop
            return self._envelope(tid, attempt, "done", arts, f"{tid} fixed")
        # impl (AI-*) writer path.
        writes = self.impl_writes.get(tid, [])
        self._write_files(cwd, writes, tid)
        arts = [{"path": w, "sha": "deadbeef"} for w in writes] or [
            {"path": f"{tid}.noop", "sha": "deadbeef"}
        ]
        return self._envelope(tid, attempt, "done", arts, f"{tid} done")


def _drive_single_review_round(clone, tmp_path, *, tasks, runner):
    """Run ONE engine dispatch of read-only review tasks inside a real
    atelier_engine window (used by #3 — a minimal probe that does NOT touch the
    full review-fix loop, so it stays orphan-deps-free)."""
    import asyncio
    import importlib

    from scripts.atelier_engine import assert_engine_available, atelier_engine

    root = assert_engine_available()
    with atelier_engine(root) as host:
        cli_dispatch = importlib.import_module("scripts.cli_dispatch")
        budget_pool_mod = importlib.import_module("scripts.budget_pool")
        result_journal_mod = importlib.import_module("scripts.result_journal")
        run_mode_mod = importlib.import_module("scripts.run_mode")

        budget = budget_pool_mod.BudgetPool(total_tokens=4_000_000)
        journal = result_journal_mod.ResultJournal(tmp_path / "probe-journal.json")
        sandbox = cli_dispatch.native_sandbox_wrap(str(clone))
        wt = host.simple_worktree_factory(clone)
        nrm = run_mode_mod.resolve_run_mode(explicit="balanced")
        return asyncio.run(
            host.run_host_pipeline_for_project(
                tasks,
                clone_dir=str(clone),
                budget=budget,
                journal=journal,
                existing_files=["seed.txt"],
                model_for=lambda _t, _a: "opus",
                briefing_for=lambda _t, _a: "Review the merged change set.",
                worktree_factory=wt,
                runner=runner,
                sandbox_wrap=sandbox,
                escalate_fn=lambda _r: None,
                run_mode=nrm,
                disallowed_tools=list(cli_dispatch.DEFAULT_DISALLOWED_TOOLS),
            )
        )


@_SKIP_ENGINE
def test_host_cycle_executor_e2e_review_clean_exit(tmp_path):
    """e2e clean SHIP through the REAL engine with a phase-aware FakeCliRunner:
    2 impl tasks merge → 2 BROADCAST reviewers (round-1: one reviewer files a
    blocker) → mesh (peer CONFIRM) → 1 fix (writer) → round-2 review is clean →
    `status=="success"`. Asserts the reviewer runner cwd is the BASE clone (C1 —
    NOT a worktree) and the clone holds the merged impl files at reviewer dispatch.

    NOTE: this drives the production review-fix loop end-to-end. It currently
    surfaces a real production defect (see the agent's bug report) — the test
    asserts the INTENDED clean-SHIP behavior; it is NOT relaxed to match a broken
    path."""
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
    runner = _PhaseAwareHostFakeRunner(
        impl_writes={"AI-1": ["scripts/foo.py"], "AI-2": ["scripts/bar.py"]},
        # Round-1: reviewer 0 (R1-0) files a blocker on foo.py; reviewer 1 (R1-1)
        # is clean.
        notes_by_task={
            "R1-0": "[blocker] scripts/foo.py:1 — needs a guard",
            "R1-1": "NO ISSUES",
            # Mesh round 1: reviewer 1 CONFIRMs reviewer 0's blocker (id R1-0-1);
            # reviewer 0 re-affirms its own (a CONFIRM of its own id keeps it).
            "M1-0": "CONFIRM R1-0-1",
            "M1-1": "CONFIRM R1-0-1",
            # Round-2 (after the fix): everyone clean → clean exit.
            "R2-0": "NO ISSUES",
            "R2-1": "NO ISSUES",
        },
        fix_writes={"FIX1-0": ["scripts/foo.py"]},
    )

    # Snapshot clone state at the moment the FIRST review task runs so we can
    # assert the merged impl files were present (C1: reviewers see the merge).
    seen_at_review: dict[str, bool] = {}

    def _snapshot(argv, cwd):
        tid, _ = runner._tid_attempt(argv)
        if tid.startswith("R") and "review-clone-snapshot" not in seen_at_review:
            seen_at_review["foo"] = (clone / "scripts" / "foo.py").exists()
            seen_at_review["bar"] = (clone / "scripts" / "bar.py").exists()
            seen_at_review["cwd_is_clone"] = Path(cwd).resolve() == clone.resolve()
            seen_at_review["review-clone-snapshot"] = True

    runner.pre_call_hook = _snapshot

    out = host_cycle_executor(
        action_items=items,
        existing_files=frozenset({"seed.txt"}),
        clone_dir=clone,
        # disjoint reviewer pool: implementers are backend-engineer-1; the two
        # extra roster roles are the disjoint reviewers.
        roster=["backend-engineer-1", "security-engineer-1", "software-architect-1"],
        subject="clean ship",
        runner=runner,
        journal_path=tmp_path / "journal.json",
        review=True,
        test_command="true",  # M8a-2c: no-op CI gate — this is a review-loop test
    )

    assert out["status"] == "success", f"expected clean SHIP, got {out!r}"
    assert out["subject"] == "clean ship"
    # M8a-2c: a clean SHIP through the review loop now also COMMITS (self-contained).
    assert re.match(r"^[0-9a-f]{40}$", out["commit_sha"]), f"not a real sha: {out['commit_sha']!r}"
    # C1: at first reviewer dispatch the merged impl files were in the BASE clone,
    # and the reviewer ran in the clone (NOT an isolated worktree).
    assert seen_at_review.get("foo") is True
    assert seen_at_review.get("bar") is True
    assert seen_at_review.get("cwd_is_clone") is True
    # Every review/mesh task ran in the base clone (no worktree carved).
    review_cwds = [
        c["cwd"]
        for c in runner.calls
        if runner._tid_attempt(c["argv"])[0].startswith(("R", "M", "PM"))
    ]
    assert review_cwds, "no review/mesh/PM tasks were dispatched"
    for cwd in review_cwds:
        assert Path(cwd).resolve() == clone.resolve()


@_SKIP_ENGINE
def test_host_cycle_executor_e2e_review_finding_on_nonexistent_file(tmp_path):
    """REGRESSION (M8b live crash, production caller): drive the REAL engine through
    Phase-4 success → round-1 review where a reviewer files a blocker referencing a
    NON-EXISTENT suggested test file ('tests/test_divide.py') → the MESH round.

    Before the fix `build_mesh_tasks` derived the mesh task `reads` from the
    finding file-refs, so the mesh DAG declared an unsatisfiable read of the
    non-existent file → the engine's reads-satisfiable gate raised an UNCAUGHT
    UnsatisfiableReadsError (exit 1, empty stdout). After the fix the mesh `reads`
    are the impl WRITES (satisfiable via the augmented review_existing), so the
    loop runs to a well-formed outcome. Mut: the old finding-ref reads path raises
    UnsatisfiableReadsError before any outcome dict is produced."""
    clone = _git_init_clone(tmp_path)
    items = [
        {
            "id": "AI-1",
            "touches": ["mathlib/divide.py"],
            "reads": [],
            "depends_on": [],
            "wave": 1,
            "owner": "backend-engineer-1",
        },
    ]
    runner = _PhaseAwareHostFakeRunner(
        impl_writes={"AI-1": ["mathlib/divide.py"]},
        notes_by_task={
            # Round-1: reviewer 0 files a blocker on a NON-EXISTENT suggested test
            # file; reviewer 1 is clean. This is the exact live shape that crashed
            # the mesh round.
            "R1-0": "[blocker] tests/test_divide.py:1 — add a divide-by-zero test",
            "R1-1": "NO ISSUES",
            # Mesh: reviewer 1 CONFIRMs reviewer 0's blocker (id R1-0-1).
            "M1-0": "CONFIRM R1-0-1",
            "M1-1": "CONFIRM R1-0-1",
            # Round-2 (after the fix writes the test file): clean → clean exit.
            "R2-0": "NO ISSUES",
            "R2-1": "NO ISSUES",
        },
        # The fix-round writer creates the suggested test file (the blocker's file).
        fix_writes={"FIX1-0": ["tests/test_divide.py"]},
    )

    out = host_cycle_executor(
        action_items=items,
        existing_files=frozenset({"seed.txt"}),
        clone_dir=clone,
        roster=["backend-engineer-1", "security-engineer-1", "software-architect-1"],
        subject="finding on nonexistent file",
        runner=runner,
        journal_path=tmp_path / "journal.json",
        review=True,
        test_command="true",  # no-op CI gate — this is a review-loop test
    )

    # The crux: the production caller did NOT raise UnsatisfiableReadsError; it
    # returned a well-formed outcome dict. (Mesh ran — assert it was dispatched.)
    assert isinstance(out, dict)
    assert out.get("status") in {"success", "abandoned"}, f"unexpected outcome: {out!r}"
    assert out["status"] == "success", f"expected clean SHIP after fix, got {out!r}"
    assert out["subject"] == "finding on nonexistent file"
    # The MESH round actually ran (M*-* tasks dispatched) — proves we exercised the
    # build_mesh_tasks path that previously crashed.
    mesh_dispatched = [c for c in runner.calls if runner._tid_attempt(c["argv"])[0].startswith("M")]
    assert mesh_dispatched, "mesh round was never dispatched — regression not exercised"


@_SKIP_ENGINE
def test_host_cycle_executor_e2e_review_unrecoverable(tmp_path):
    """e2e review_unrecoverable through the REAL engine: a PERSISTENT blocker that
    the fix never clears, with the PM REJECTing every round, exhausts
    MAX_ITERATIONS → `review_unrecoverable` abandonment with all FOUR review fields
    populated and folded into the outcome. Mut: any break in the round sequence, or
    a failure to fold the abandonment fields, would not produce this dict.

    NOTE: drives the production loop end-to-end; surfaces the same defect as #17 —
    the test asserts the INTENDED unrecoverable outcome, not a broken path."""
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
    ]
    runner = _PhaseAwareHostFakeRunner(
        impl_writes={"AI-1": ["scripts/foo.py"]},
        # EVERY round-1 review (any iteration) re-files the same blocker; the mesh
        # always CONFIRMs it; the fix "succeeds" (writes) but the next round still
        # finds the blocker → persistent. The PM REJECTs every round (default).
        notes_by_prefix={
            "R1": "[blocker] scripts/foo.py:1 — persistent issue",
            "R2": "[blocker] scripts/foo.py:1 — persistent issue",
            "R3": "[blocker] scripts/foo.py:1 — persistent issue",
            "R4": "[blocker] scripts/foo.py:1 — persistent issue",
            "R5": "[blocker] scripts/foo.py:1 — persistent issue",
            "M1": "CONFIRM R1-0-1",
            "M2": "CONFIRM R2-0-1",
            "M3": "CONFIRM R3-0-1",
            "M4": "CONFIRM R4-0-1",
            "M5": "CONFIRM R5-0-1",
        },
        fix_writes={f"FIX{i}-0": ["scripts/foo.py"] for i in range(1, 6)},
        pm_default="REJECT — this must be fixed",
    )

    out = host_cycle_executor(
        action_items=items,
        existing_files=frozenset({"seed.txt"}),
        clone_dir=clone,
        roster=["backend-engineer-1", "security-engineer-1", "software-architect-1"],
        subject="persistent blocker",
        runner=runner,
        journal_path=tmp_path / "journal.json",
        review=True,
    )

    assert out["status"] == "abandoned", f"expected review_unrecoverable, got {out!r}"
    assert out["reason"] == "review_unrecoverable"
    assert out["phase_reached"] == "review"
    # All FOUR review-outcome fields populated (folded from build_abandonment_outcome).
    assert out["review_iteration_count"] is not None
    assert out["review_iteration_count"] >= 1
    assert out["unresolved_findings"]  # non-empty list of dicts
    assert isinstance(out["unresolved_findings"], list)
    assert isinstance(out["unresolved_findings"][0], dict)
    assert out["convergence_summary"]  # non-empty human summary
    assert out["reviewer_attribution"]  # finding_id -> reviewer map, non-empty


@_SKIP_ENGINE
def test_consolidate_mesh_map_is_consumed_not_discarded(tmp_path):
    """#10 (LOW-1) — drive the REAL engine to fix-loop exhaustion where reviewer-0
    files a blocker that reviewer-1 NEVER cross-confirms in the mesh (reviewer-1's
    mesh reply is an unrelated net-new nit, so it carries signal but no CONFIRM of
    the blocker). The blocker survives (blocker/major always retained), the PM
    REJECTs every round → exhaustion → the abandonment's convergence_summary names
    the peer-unconfirmed blocker id via the neutral clause. Mut (re-introduce the
    `_consolidate_mesh` side-map discard): the clause / id would be absent.

    This proves the side-map flows ALL the way from `_consolidate_mesh` →
    `_run_review_fix_loop` → `build_abandonment_outcome` → convergence_summary —
    the wiring the loop previously discarded at the binders."""
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
    ]
    # Each round-1: reviewer-0 (R{i}-0) files the blocker; reviewer-1 (R{i}-1)
    # finds nothing. The per-reviewer round-1 replies MUST be keyed per EXACT
    # task_id (`notes_by_task`) — `notes_by_prefix` keys on the round prefix
    # (`R{i}`), which both reviewers share, so it cannot differentiate them. In the
    # mesh ONLY reviewer-1 has a peer to review (reviewer-0's blocker) — it replies
    # with an UNRELATED net-new nit (recognized signal, but NO CONFIRM of the
    # blocker id) → the blocker stays peer-unconfirmed. The fix "succeeds" each
    # round but the next round re-files the blocker → persistent → exhaustion.
    notes_by_task = {}
    notes_by_prefix = {}
    for i in range(1, 6):
        notes_by_task[f"R{i}-0"] = f"[blocker] scripts/foo.py:1 — round {i} blocker"
        notes_by_task[f"R{i}-1"] = "NO ISSUES"
        # Mesh: reviewer-1 (the only dispatched mesh reviewer) emits a net-new NIT,
        # never a CONFIRM of the blocker id → confirms==0 → peer_unconfirmed.
        notes_by_prefix[f"M{i}"] = "[nit] scripts/foo.py:9 — minor style nit"
    runner = _PhaseAwareHostFakeRunner(
        impl_writes={"AI-1": ["scripts/foo.py"]},
        notes_by_task=notes_by_task,
        notes_by_prefix=notes_by_prefix,
        fix_writes={f"FIX{i}-0": ["scripts/foo.py"] for i in range(1, 6)},
        pm_default="REJECT — keep iterating",
    )

    out = host_cycle_executor(
        action_items=items,
        existing_files=frozenset({"seed.txt"}),
        clone_dir=clone,
        roster=["backend-engineer-1", "security-engineer-1", "software-architect-1"],
        subject="peer-unconfirmed blocker",
        runner=runner,
        journal_path=tmp_path / "journal.json",
        review=True,
        test_command="true",
    )

    assert out["status"] == "abandoned", f"expected review_unrecoverable, got {out!r}"
    assert out["reason"] == "review_unrecoverable"
    summary = out["convergence_summary"]
    # The neutral peer-unconfirmed clause is present and names the LATEST round's
    # blocker id (iteration 5 → R5-0-0). This is the discarded side-map, now wired.
    assert "Not peer-confirmed (single-reviewer):" in summary, (
        f"peer-unconfirmed clause missing from convergence_summary: {summary!r}"
    )
    # Round-1 finding ids are stamped R{iter}-{reviewer_idx}-{k} with k starting at
    # 1, so the latest round's (iteration 5) reviewer-0 blocker is R5-0-1.
    assert "R5-0-1" in summary, f"peer-unconfirmed blocker id not named: {summary!r}"


@_SKIP_ENGINE
def test_non_routable_blocker_drives_review_unrecoverable_not_silent_success(tmp_path):
    """M8b Bug#4 e2e regression. A single reviewer files a blocker whose target is
    a DIRECTORY (`tests/`) every round. It can NEVER be auto-fixed by a file-owner
    writer (a directory is not a valid `writes` target → would corrupt the engine's
    worktree carving). The fix must NOT (a) crash by feeding a directory into
    `writes`, nor (b) silently drop the blocker (which would falsely converge the
    review loop to success). Instead the non-routable blocker persists as an
    UNRESOLVED finding → the loop terminates at MAX_ITERATIONS → a clean
    `review_unrecoverable` abandonment whose convergence_summary NAMES it.

    Mut (pre-fix): `_coalesce_blockers_by_file` yields `{"tests/": [...]}`,
    `build_fix_tasks` emits `writes=["tests/"]`, the engine merge conflicts → the
    cycle abandons via the WorktreeError catch (NOT review_unrecoverable) — a
    success-RATE bug. Mut (silent-drop): zero blockers → status=='success'."""
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
    ]
    # ONE reviewer files a DIRECTORY-targeted blocker every round (single reviewer →
    # mesh is vacuous). No fix can ever resolve it → persists → exhaustion.
    notes_by_task = {f"R{i}-0": "[blocker] tests/ — whole test tree is broken" for i in range(1, 6)}
    runner = _PhaseAwareHostFakeRunner(
        impl_writes={"AI-1": ["scripts/foo.py"]},
        notes_by_task=notes_by_task,
        pm_default="REJECT — keep iterating",
    )

    out = host_cycle_executor(
        action_items=items,
        existing_files=frozenset({"seed.txt"}),
        clone_dir=clone,
        roster=["backend-engineer-1", "security-engineer-1"],
        subject="non-routable blocker",
        runner=runner,
        journal_path=tmp_path / "journal.json",
        review=True,
        test_command="true",
    )

    assert out["status"] == "abandoned", f"must NOT silently succeed: {out!r}"
    assert out["reason"] == "review_unrecoverable", (
        f"non-routable blocker must drive review_unrecoverable, not a worktree crash: {out!r}"
    )
    # The non-routable blocker is surfaced in the unresolved findings + summary.
    unresolved = out["unresolved_findings"]
    assert any(f["file_line"] == "tests/" for f in unresolved), (
        f"non-routable blocker dropped from unresolved_findings: {unresolved!r}"
    )
    assert "Non-routable" in out["convergence_summary"], (
        f"non-routable disclosure missing: {out['convergence_summary']!r}"
    )


def test_review_terminal_rule_placeholder_wording():
    """#11 (LOW-2) — the read-only review terminal rule names
    `review-noop.placeholder` (matching the FakeCliRunner's `{tid}.noop` no-write
    artifact convention), NOT `review-notes.md`, and explains the path is an inert
    schema placeholder that no consumer reads. Mut (revert prose): the old
    `review-notes.md` wording / missing placeholder clause fails these asserts."""
    assert "review-noop.placeholder" in _REVIEW_TERMINAL_RULE
    assert "review-notes.md" not in _REVIEW_TERMINAL_RULE
    assert "schema placeholder" in _REVIEW_TERMINAL_RULE
    assert "no consumer reads it" in _REVIEW_TERMINAL_RULE
    assert "verdict lives entirely in `notes_md`" in _REVIEW_TERMINAL_RULE


# ── M8a-2c #3/#4 — self-contained commit + journal-survives-commit (e2e) ─────


@_SKIP_ENGINE
def test_host_cycle_executor_e2e_commits_and_returns_sha(tmp_path):
    """#3 — drive the REAL engine with a FakeCliRunner; on a clean Phase-4
    success the executor COMMITS the merged work and returns a real 40-hex
    commit_sha (+ Memex slug). git log confirms the kaizen(cycle-N) commit holds
    the impl files. Mut: a wrapper that does NOT commit leaves commit_sha=None and
    HEAD on the `init` commit (no kaizen subject, no impl files staged)."""
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
    runner = _HonestHostFakeRunner({"AI-1": ["scripts/foo.py"], "AI-2": ["scripts/bar.py"]})
    out = host_cycle_executor(
        action_items=items,
        existing_files=frozenset({"seed.txt"}),
        clone_dir=clone,
        roster=["backend-engineer-1", "sdet-1"],
        subject="commit me",
        runner=runner,
        journal_path=tmp_path / "journal.json",
        review=False,
        cycle_n=2,
        run_id=7,
        test_command="true",  # no-op gate — this test exercises the COMMIT path
    )
    assert out["status"] == "success", f"expected success, got {out!r}"
    # Real 40-hex SHA + Memex slug stamped by the self-contained finalizer.
    assert re.match(r"^[0-9a-f]{40}$", out["commit_sha"]), f"not a real sha: {out['commit_sha']!r}"
    assert out["minutes_memex_slug"] == "kaizen:cycle:7-2"
    # git log: HEAD is the kaizen cycle commit (its sha matches out["commit_sha"]).
    head = subprocess.run(
        ["git", "-C", str(clone), "log", "-1", "--pretty=%H%n%s"],
        capture_output=True,
        text=True,
        check=True,
    )
    head_sha, head_subject = head.stdout.strip().splitlines()
    assert head_sha == out["commit_sha"]
    assert head_subject == "kaizen(cycle-2): host-mode cycle"
    # The engine eager-merges each impl writer into HEAD as its own commit, so
    # the kaizen cycle commit itself is empty (a metadata stamp). The merged
    # impl files are TRACKED in HEAD's tree and present on disk regardless.
    tracked = subprocess.run(
        ["git", "-C", str(clone), "ls-tree", "-r", "--name-only", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "scripts/foo.py" in tracked.stdout
    assert "scripts/bar.py" in tracked.stdout
    assert (clone / "scripts" / "foo.py").exists()
    assert (clone / "scripts" / "bar.py").exists()


@_SKIP_ENGINE
def test_host_cycle_executor_journal_survives_commit(tmp_path):
    """#4 — with the DEFAULT journal path (no journal_path arg), a clean success
    commits AND the journal still exists afterward. Mut (§1A journal-wipe): if the
    default journal lived at clone/.ai/host-journal.json, commit_cycle →
    _strip_transient_dirs would rmtree clone/.ai/ and DELETE the journal; the
    assertion `clone/.ai/host-journal.json does NOT exist` proves the default
    journal is OUTSIDE the clone, and the success outcome proves the commit ran."""
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
    ]
    runner = _HonestHostFakeRunner({"AI-1": ["scripts/foo.py"]})
    out = host_cycle_executor(
        action_items=items,
        existing_files=frozenset({"seed.txt"}),
        clone_dir=clone,
        roster=["backend-engineer-1", "sdet-1"],
        subject="journal survives",
        runner=runner,
        # NO journal_path → exercises the DEFAULT (must resolve OUTSIDE the clone).
        review=False,
        cycle_n=1,
        test_command="true",
    )
    assert out["status"] == "success", f"expected success, got {out!r}"
    assert re.match(r"^[0-9a-f]{40}$", out["commit_sha"])
    # NON-VACUOUS journal-survival check: the DEFAULT journal must EXIST at its
    # deterministic out-of-clone location AFTER the commit. Mut (§1A): if the
    # default regressed back to clone/.ai/host-journal.json, commit_cycle's
    # _strip_transient_dirs would rmtree clone/.ai/ → this path would NOT exist →
    # RED. (Asserting only that the journal is ABSENT from the clone is vacuous —
    # a DELETED journal satisfies that too.)
    expected_journal = clone.parent / f"{clone.name}.host-journal.json"
    assert expected_journal.exists(), (
        f"default journal must SURVIVE the commit at {expected_journal} "
        "(outside the clone, untouched by the transient-dir strip)"
    )
    # And it must NOT have been placed inside the clone's stripped .ai/.
    assert not (clone / ".ai" / "host-journal.json").exists(), (
        "default journal must live OUTSIDE the clone — clone/.ai is stripped on commit"
    )
    # And the commit did not stage any stray .ai/ journal artifact.
    names = subprocess.run(
        ["git", "-C", str(clone), "show", "--name-only", "--pretty=format:"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert not any(line.startswith(".ai/") for line in names.stdout.splitlines())


# ── M8a-2c #5/#6/#7 — CI-mirror gate (pure, no engine) ──────────────────────


def _ruff_clone(tmp_path: Path, py_body: str) -> Path:
    """A committed clone that opts into ruff (pyproject `[tool.ruff]`) with one
    .py file. ``py_body`` controls whether ruff passes (clean) or fails (e.g. an
    unused import → F401)."""
    clone = tmp_path / "rclone"
    clone.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "PATH": __import__("os").environ.get("PATH", ""),
    }
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=clone, env=env, check=True)
    (clone / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n")
    (clone / "mod.py").write_text(py_body)
    subprocess.run(["git", "add", "-A"], cwd=clone, env=env, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=clone, env=env, check=True)
    return clone


@_SKIP_NO_RUFF
def test_host_ci_gate_pre_existing_failure_does_not_abandon(tmp_path):
    """#5 — a pre-existing ruff break (present in BOTH baseline and gate) must NOT
    abandon: it predates the cycle's edits. Mut (drop the baseline diff): without
    the baseline the pre-existing fail reads as cycle-introduced → abandon.

    We build the baseline by running the SAME real ruff gate over the broken
    clone, so baseline.ruff_check == fail; the gate then sees the identical fail
    and `_diff_ci_results` classifies it pre-existing → returns None (proceed)."""
    clone = _ruff_clone(tmp_path, "import os\n")  # F401 — pre-existing break
    # Baseline captured with test_command="true" (real ruff still runs).
    _, ci_baseline = run_ci_checks(clone, "true")
    assert ci_baseline["ruff_check"]["status"] == "fail"  # the break IS in baseline
    last_ci_results, abandon = _run_ci_gate(
        clone,
        "true",
        ci_baseline,
        subject="pre-existing debt",
        participants=["backend-engineer-1"],
    )
    assert abandon is None, f"pre-existing failure must NOT abandon, got {abandon!r}"
    assert last_ci_results["ruff_check"]["status"] == "fail"  # still observed, just ignored


@_SKIP_NO_RUFF
def test_host_ci_gate_cycle_introduced_failure_abandons(tmp_path):
    """#6 — a ruff break that is ABSENT from the baseline but present at the gate
    (the cycle introduced it) abandons with phase_reached='test',
    reason='lint_failed'. Mut: gate not running (no abandon) or wrong reason
    (anything but lint_failed) fails the assertions."""
    clone = _ruff_clone(tmp_path, "x = 1\n")  # clean at baseline
    _, ci_baseline = run_ci_checks(clone, "true")
    assert ci_baseline["ruff_check"]["status"] == "pass"  # baseline is clean
    # Now the "cycle" introduces an F401 break before the gate runs.
    (clone / "mod.py").write_text("import os\n")
    _last_ci_results, abandon = _run_ci_gate(
        clone,
        "true",
        ci_baseline,
        subject="introduced break",
        participants=["backend-engineer-1"],
    )
    assert abandon is not None, "cycle-introduced ruff break must abandon"
    assert abandon["status"] == "abandoned"
    assert abandon["phase_reached"] == "test"
    assert abandon["reason"] == "lint_failed"
    assert "ruff_check" in abandon["detail"]
    # Shape parity: the four review-outcome keys are present and None.
    for k in (
        "review_iteration_count",
        "unresolved_findings",
        "convergence_summary",
        "reviewer_attribution",
    ):
        assert abandon[k] is None


def test_host_ci_abandon_reason_parity_exact(tmp_path):
    """#7 — every CI check maps to the SAME abandonment reason host and team modes
    use (via the shared `_pick_highest_reason`/`_CHECK_TO_REASON`). Parametrized
    by faking a single-check fail and asserting the gate's reason equals the
    canonical map. Mut: a mis-map (e.g. bandit→lint_failed) fails the exact-equality
    assertion against `_CHECK_TO_REASON`.

    Driven through `_run_ci_gate` with a monkeypatched `run_ci_checks` so we
    control exactly one failing check at a time (no need to manufacture a real
    bandit/pip-audit break)."""
    import scripts.host_executor as he

    cases = {
        "ruff_check": "lint_failed",
        "ruff_format": "lint_failed",
        "bandit": "security_failed",
        "pip_audit": "sca_failed",
        "tests": "tests_unrecoverable",
    }
    # Anchor the expectation against the canonical map (not a hand-copy):
    # any divergence between this test's table and _CHECK_TO_REASON is a bug in one
    # of them, so assert they agree first.
    assert cases == _CHECK_TO_REASON, (
        f"parity table drifted from fix_loop._CHECK_TO_REASON: {cases} != {_CHECK_TO_REASON}"
    )
    clone = tmp_path / "c"
    clone.mkdir()
    for check, expected_reason in cases.items():

        def fake_run_ci_checks(_clone, _cmd, _check=check):
            results = {
                "tests": {"status": "pass", "output": "=== 1 passed in 0.01s ==="},
                "ruff_check": {"status": "pass", "output": ""},
                "ruff_format": {"status": "pass", "output": ""},
                "bandit": {"status": "pass", "output": ""},
                "pip_audit": {"status": "pass", "output": ""},
            }
            results[_check] = {"status": "fail", "output": f"{_check} broke"}
            return False, results

        orig = he.run_ci_checks
        he.run_ci_checks = fake_run_ci_checks
        try:
            # Baseline = all green (the break is cycle-introduced).
            baseline = {name: {"status": "pass", "output": ""} for name in cases}
            _, abandon = _run_ci_gate(
                clone,
                "pytest",
                baseline,
                subject="parity",
                participants=["backend-engineer-1"],
            )
        finally:
            he.run_ci_checks = orig
        assert abandon is not None, f"{check} fail must abandon"
        assert abandon["reason"] == expected_reason, (
            f"check {check!r} mapped to {abandon['reason']!r}, expected {expected_reason!r}"
        )
        assert abandon["phase_reached"] == "test"


# ── LIVE e2e — real `claude` CLI through the sandbox (deselected by default) ──
#
# Drives `host_cycle_executor(review=True)` end-to-end with the PRODUCTION caller
# (`runner=None` → atelier's real `real_cli_runner` resolved in-window) on a tiny
# real git repo. NEVER sets ATELIER_CLI_ALLOW_UNSANDBOXED — the cycle runs fully
# sandboxed (native_sandbox_wrap, wired in host_cycle_executor). Marked `live`
# (CI deselects via `addopts = "-m 'not live'"`) AND skipped unless the engine +
# `claude` + a native sandbox are all present. Run explicitly:
#   PYTHONPATH=. python3 -m pytest -m live tests/test_host_executor.py -v
#
# Per `feedback-test-the-production-caller-not-just-units`: the FakeCliRunner can
# never exercise the real CONFIRM/RETRACT/ESCALATE grammar, the real review
# prompts, or the journal-replay path — only a live run does. Assertions are
# robust to LLM nondeterminism (a real opus reviewer may legitimately file a
# persistent blocker → `review_unrecoverable` is a VALID outcome).
@pytest.mark.live
@_LIVE
def test_host_cycle_executor_live_review_cycle(tmp_path):
    clone = _git_init_clone(tmp_path)
    items = [
        {
            "id": "AI-1",
            "touches": ["greeting.py"],
            "reads": [],
            "depends_on": [],
            "wave": 1,
            "owner": "backend-engineer-1",
            # The task body the Phase-4 briefing renders for the real implementer.
            "title": "Create greeting.py",
            "description": (
                "Create a new file `greeting.py` at the repo root containing a "
                "single function `greet(name: str) -> str` that returns the "
                "string `Hello, {name}!` (f-string). Keep it minimal — no extra "
                "imports, no __main__ block. The file must be valid Python."
            ),
        },
    ]
    roster = ["backend-engineer-1", "security-engineer-1", "software-architect-1"]

    out = host_cycle_executor(
        action_items=items,
        existing_files=frozenset({"seed.txt"}),
        clone_dir=clone,
        roster=roster,
        pm="pm-1",
        subject="live host review cycle",
        runner=None,  # → REAL real_cli_runner resolved in-window (production caller)
        journal_path=tmp_path / "journal.json",  # OUTSIDE the clone (avoids the strip hazard)
        review=True,
        cycle_n=1,
        test_command="true",  # no-op CI gate (always-pass) — this validates the REVIEW loop, not CI
    )

    # ── anchor: the call returned a well-formed outcome (no exception escaped) ─
    assert isinstance(out, dict)
    assert out["status"] in {"success", "abandoned"}, f"unexpected status: {out!r}"
    assert out["subject"] == "live host review cycle"

    if out["status"] == "success":
        # The M8a-2c flip: commit_sha is now a REAL 40-char hex sha (was None).
        sha = out["commit_sha"]
        assert isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha), f"bad sha: {sha!r}"
        assert (out.get("minutes_memex_slug") or "").startswith("kaizen:cycle:")
        assert out["participants"] == roster
        # The engine eager-merges impl work into HEAD's TREE; the kaizen cycle
        # commit is an empty metadata stamp ON TOP. So assert the impl file lives
        # in HEAD's tree (git ls-tree) + on disk — NOT in `git show HEAD` (the
        # stamp is empty).
        tree = subprocess.run(
            ["git", "-C", str(clone), "ls-tree", "-r", "--name-only", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "greeting.py" in tree, f"greeting.py not in HEAD tree:\n{tree}"
        assert (clone / "greeting.py").exists()
    else:
        # A real reviewer may legitimately block; CI ran no-op so test reasons are
        # excluded. Accept the review/impl-phase abandonment reasons.
        assert out["reason"] in {"no_consensus", "review_unrecoverable", "other"}, (
            f"unexpected abandon reason: {out!r}"
        )
        if out["reason"] == "review_unrecoverable":
            # The four review-outcome fields must be present on this path.
            for k in (
                "review_iteration_count",
                "unresolved_findings",
                "convergence_summary",
                "reviewer_attribution",
            ):
                assert k in out, f"missing {k} on review_unrecoverable outcome"


# ════════════════════════════════════════════════════════════════════════════
# M8b — engine WorktreeError → graceful abandon (not an uncaught traceback).
#
# atelier's engine raises `scripts.host_scheduler.WorktreeError` when a worktree
# merge fails (the M8b live-e2e finding: a CRLF-dirty base tree makes the engine's
# `git merge --no-ff` of a file-MODIFYING worktree refuse). host_cycle_executor
# must CATCH that engine class IN-WINDOW and convert it to a clean kaizen abandon
# dict — NOT let it propagate as an exit-1 traceback with empty stdout.
# ════════════════════════════════════════════════════════════════════════════


@_SKIP_ENGINE
def test_host_cycle_executor_engine_worktree_error_abandons_gracefully(tmp_path, monkeypatch):
    """A WorktreeError raised by the engine's in-window merge MUST be converted to
    a clean abandon dict — host_cycle_executor must NOT re-raise.

    We force the failure at the seam the engine actually calls: atelier's
    in-window `scripts.host_scheduler._merge_worktree`. Because the engine swap
    window RE-IMPORTS atelier's `scripts.*` fresh on every entry (and purges it on
    exit — see scripts.atelier_engine), a patch applied in a transient window does
    not survive into the executor's own window. So we wrap the `atelier_engine`
    context manager the executor imports and patch `_merge_worktree` on the
    FRESHLY-yielded host_scheduler module, AND bind `WorktreeError` from that same
    in-window module (it is an ATELIER class — `from scripts.host_scheduler import
    WorktreeError` at the kaizen module top would resolve to kaizen, where the
    class does not exist).

    AI-1 is a single wave-1 writer with a real declared write, so it reaches the
    eager-merge step; the patched `_merge_worktree` raises there. Pre-fix:
    host_cycle_executor does NOT catch WorktreeError → it propagates (uncaught) and
    this test RAISES. Post-fix: a graceful abandon dict is RETURNED.
    """
    import contextlib

    from scripts import atelier_engine as _ae_mod
    from scripts import host_executor as _host_mod

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
    ]
    runner = _HonestHostFakeRunner({"AI-1": ["scripts/foo.py"]})

    real_atelier_engine = _ae_mod.atelier_engine

    @contextlib.contextmanager
    def _patching_engine(atelier_root=None):
        # Delegate to the real swap; patch the FRESH in-window host_scheduler so
        # its merge step raises the in-window WorktreeError class.
        with real_atelier_engine(atelier_root) as host_scheduler:
            worktree_error = host_scheduler.WorktreeError

            def _boom(_wt):
                raise worktree_error(
                    "INVARIANT VIOLATION: merging worktree conflicted (simulated "
                    "CRLF-dirty base tree) — local changes would be overwritten"
                )

            monkeypatch.setattr(host_scheduler, "_merge_worktree", _boom)
            yield host_scheduler

    # host_executor calls `atelier_engine(...)` via its module-level import, so
    # patch the name in the host_executor namespace.
    monkeypatch.setattr(_host_mod, "atelier_engine", _patching_engine)

    out = host_cycle_executor(
        action_items=items,
        existing_files=frozenset({"seed.txt"}),
        clone_dir=clone,
        roster=["backend-engineer-1", "sdet-1"],
        subject="worktree boom",
        runner=runner,
        journal_path=tmp_path / "journal.json",
        review=False,
        test_command="true",
    )

    # ── graceful abandon (NOT a raised traceback) ───────────────────────────
    assert isinstance(out, dict), f"expected a dict outcome, got {type(out)!r}"
    assert out["status"] == "abandoned", f"expected abandoned, got {out!r}"
    assert out["phase_reached"] == "implementation", out
    assert out["reason"] in VALID_REASONS, f"reason {out['reason']!r} not a valid taxonomy slot"
    # The engine message rides through into `detail`.
    assert "worktree" in out["detail"].lower(), out["detail"]
    assert out["subject"] == "worktree boom"
    assert out["participants"] == ["backend-engineer-1", "sdet-1"]
    assert "artifacts" in out
