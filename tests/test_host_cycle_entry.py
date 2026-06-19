"""Tests for the headless ``KAIZEN_TRANSPORT=host`` cycle entrypoint (M8 glue).

Per ``feedback-test-the-production-caller-not-just-units``: the M8a units passed
while the host path was UNREACHABLE from the top-level command. So these tests
drive the PRODUCTION caller — ``scripts.host_cycle_entry.run_host_cycle`` (and the
``main`` argv path) — end-to-end, not just helper internals.

Coverage:
  * transport guard REJECTS when ``KAIZEN_TRANSPORT`` resolves to something other
    than ``host`` (explicit ``bridge`` / unknown), BEFORE any executor call — no
    engine needed. NB (M8c): unset now DEFAULTS to host, so an unset env RUNS the
    host entry rather than rejecting (see the e2e default-unset test).
  * an engine-shaped / malformed DAG FAILS FAST with ``ActionItemsShapeError``
    (the architect's RISK-1 — must not silently become ``no_consensus``).
  * a clean kaizen-native DAG → success outcome with a POPULATED 40-hex
    ``commit_sha`` (the executor committed internally) + the host Memex slug; the
    DAG is passed through UNCHANGED to the executor.
  * the ``main`` argv path emits the outcome dict as JSON on stdout and exits 0;
    a guard rejection exits 2 with a stderr message.

Engine-touching tests SKIP cleanly when atelier (>=1.10.0) is absent.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from scripts.host_cycle_entry import (
    ActionItemsShapeError,
    main,
    run_host_cycle,
)
from scripts.transport import UnknownTransportError, require_wired_transport

# Reuse the honest FakeCliRunner + git-clone helper from the executor tests —
# the entry's e2e drives the SAME engine through the SAME fake, just via the
# production entry caller rather than host_cycle_executor directly.
from tests.test_host_executor import (
    _SKIP_ENGINE,
    _git_init_clone,
    _PhaseAwareHostFakeRunner,
)

_HOST_ENV = {"KAIZEN_TRANSPORT": "host"}


# ── a kaizen-native 2-wave DAG fixture (shared by the e2e tests) ─────────────


def _native_dag() -> list[dict]:
    return [
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
        {
            "id": "AI-3",
            "touches": ["tests/test_foo.py"],
            "reads": ["scripts/foo.py"],
            "depends_on": ["AI-1"],
            "wave": 2,
            "owner": "sdet-1",
        },
    ]


def _write_dag(tmp_path: Path, items) -> Path:
    p = tmp_path / "host_action_items.json"
    p.write_text(json.dumps(items), encoding="utf-8")
    return p


# ── RISK-4: the host relaxation is SCOPED to this entrypoint ─────────────────


def test_require_wired_transport_run_py_slot_still_rejects_host():
    """The run.py Python-cycle-executor contract (allow_host=False, the DEFAULT)
    MUST still raise for host — the relaxation is scoped to host_cycle_entry, NOT
    global (RISK-4). A future run.py caller that forgets M8c factoring fails loud."""
    with pytest.raises(NotImplementedError):
        require_wired_transport({"KAIZEN_TRANSPORT": "host"})  # allow_host defaults False


def test_require_wired_transport_entry_contract_allows_host():
    """The host_cycle_entry contract (allow_host=True) resolves host cleanly."""
    assert require_wired_transport({"KAIZEN_TRANSPORT": "host"}, allow_host=True) == "host"


def test_require_wired_transport_bridge_explicit_both_ways():
    """The explicit bridge opt-out resolves normally regardless of allow_host."""
    assert require_wired_transport({"KAIZEN_TRANSPORT": "bridge"}) == "bridge"
    assert require_wired_transport({"KAIZEN_TRANSPORT": "bridge"}, allow_host=True) == "bridge"


def test_require_wired_transport_default_allowed_resolves_host():
    """M8c: unset now defaults to host. allow_host=True (the host_cycle_entry
    contract) resolves the default cleanly to host."""
    assert require_wired_transport({}, allow_host=True) == "host"


# ── transport guard (no engine needed — fails before any executor call) ──────


def test_entry_rejects_bridge_transport(tmp_path):
    """KAIZEN_TRANSPORT=bridge must NOT run the host entry (NotImplementedError)."""
    dag = _write_dag(tmp_path, _native_dag())
    with pytest.raises(NotImplementedError):
        run_host_cycle(
            action_items_file=dag,
            clone_dir=tmp_path,
            subject="x",
            roster=["backend-engineer-1"],
            pm=None,
            cycle_n=1,
            run_id=None,
            test_command="true",
            env={"KAIZEN_TRANSPORT": "bridge"},
        )


# NOTE (M8c): unset KAIZEN_TRANSPORT now DEFAULTS to host, so the entry no longer
# rejects an unset env — see `test_entry_e2e_default_unset_routes_to_host` in the
# e2e section below, which drives the unset-env default end-to-end through the
# engine. The transport-guard contract for the unset default is also covered by
# `test_require_wired_transport_default_allowed_resolves_host` above.


def test_entry_rejects_unknown_transport(tmp_path):
    """A typo'd KAIZEN_TRANSPORT must fail loud, not silently default."""
    dag = _write_dag(tmp_path, _native_dag())
    with pytest.raises(UnknownTransportError):
        run_host_cycle(
            action_items_file=dag,
            clone_dir=tmp_path,
            subject="x",
            roster=["backend-engineer-1"],
            pm=None,
            cycle_n=1,
            run_id=None,
            test_command="true",
            env={"KAIZEN_TRANSPORT": "bogus"},
        )


# ── fail-fast on the wrong DAG shape (no engine needed) ──────────────────────


def test_entry_fails_fast_on_engine_shaped_keys(tmp_path):
    """Engine-OUTPUT keys (task_id/parallel_group/writes/...) must FAIL FAST with a
    clear error — NOT silently become a no_consensus abandon (RISK-1)."""
    engine_shaped = [
        {
            "task_id": "T-1",  # ← engine output key, not a native Action-Item
            "parallel_group": 0,
            "writes": ["scripts/foo.py"],
            "assigned_persona": "backend-engineer-1",
            "phase": "implementation",
        }
    ]
    dag = _write_dag(tmp_path, engine_shaped)
    with pytest.raises(ActionItemsShapeError) as ei:
        run_host_cycle(
            action_items_file=dag,
            clone_dir=tmp_path,
            subject="x",
            roster=["backend-engineer-1"],
            pm=None,
            cycle_n=1,
            run_id=None,
            test_command="true",
            env=_HOST_ENV,
        )
    # The error must name the offending engine key(s) so the agent can fix the
    # serialization, and must NOT be a no_consensus abandon dict.
    assert "engine-shaped" in str(ei.value)
    assert "task_id" in str(ei.value)


def test_entry_fails_fast_on_non_list_payload(tmp_path):
    """A non-list top-level payload is a coarse shape bug → fail fast."""
    p = tmp_path / "host_action_items.json"
    p.write_text(json.dumps({"id": "AI-1"}), encoding="utf-8")  # dict, not list
    with pytest.raises(ActionItemsShapeError):
        run_host_cycle(
            action_items_file=p,
            clone_dir=tmp_path,
            subject="x",
            roster=["backend-engineer-1"],
            pm=None,
            cycle_n=1,
            run_id=None,
            test_command="true",
            env=_HOST_ENV,
        )


def test_entry_fails_fast_on_invalid_json(tmp_path):
    """A non-JSON action-items file must surface a clear ActionItemsShapeError."""
    p = tmp_path / "host_action_items.json"
    p.write_text("not json {", encoding="utf-8")
    with pytest.raises(ActionItemsShapeError):
        run_host_cycle(
            action_items_file=p,
            clone_dir=tmp_path,
            subject="x",
            roster=["backend-engineer-1"],
            pm=None,
            cycle_n=1,
            run_id=None,
            test_command="true",
            env=_HOST_ENV,
        )


# ── end-to-end through the production entry caller (engine + FakeCliRunner) ───


# A roster with reviewers DISJOINT from the implementers (backend-engineer-1 /
# sdet-1 own the Phase-4 items), so `select_reviewers` can pick a clean review
# pool — the production `review=True` path the entry runs.
_E2E_ROSTER = [
    "backend-engineer-1",
    "sdet-1",
    "security-engineer-1",
    "software-architect-1",
]
# All reviewers report NO ISSUES at round 1 → clean exit, no mesh / no fix.
_CLEAN_REVIEW_NOTES = dict.fromkeys(("R1", "R2", "R3"), "NO ISSUES")


@_SKIP_ENGINE
def test_entry_e2e_success_commits_and_passes_dag_through(tmp_path):
    """Drive run_host_cycle END-TO-END through the PRODUCTION review=True path with a
    phase-aware FakeCliRunner: a clean kaizen-native DAG (clean reviews) yields the
    success 5-key shape with a POPULATED 40-hex commit_sha (the executor committed
    internally — F3) and the host Memex slug; the produced files land in the clone."""
    clone = _git_init_clone(tmp_path)
    items = _native_dag()
    dag = _write_dag(tmp_path, items)
    runner = _PhaseAwareHostFakeRunner(
        impl_writes={
            "AI-1": ["scripts/foo.py"],
            "AI-2": ["scripts/bar.py"],
            "AI-3": ["tests/test_foo.py"],
        },
        notes_by_prefix=_CLEAN_REVIEW_NOTES,
    )

    out = run_host_cycle(
        action_items_file=dag,
        clone_dir=clone,
        subject="entry subject",
        roster=_E2E_ROSTER,
        pm=None,
        cycle_n=1,
        run_id=None,
        # `true` no-op gate: the seed clone has no tests, so a real pytest gate is
        # noise for this dispatch/merge test (the gate's own coverage lives in
        # test_host_executor's CI-gate tests).
        test_command="true",
        env=_HOST_ENV,
        runner=runner,
    )

    # Success 5-key shape with a REAL committed sha (F3 — committed internally).
    assert out["status"] == "success", out
    assert out["subject"] == "entry subject"
    assert re.match(r"^[0-9a-f]{40}$", out["commit_sha"]), f"not a real sha: {out['commit_sha']!r}"
    assert out["minutes_memex_slug"] == "kaizen:cycle:host-1"
    assert out["participants"] == _E2E_ROSTER

    # The DAG was passed through UNCHANGED to the executor — every native impl item
    # id was dispatched (the entry did not mutate / re-key the DAG). Filter to the
    # AI-* impl dispatches (review/mesh/PM task ids also appear in runner.calls).
    impl_dispatched = sorted(
        {tid for c in runner.calls if (tid := runner._tid_attempt(c["argv"])[0]).startswith("AI-")}
    )
    assert impl_dispatched == ["AI-1", "AI-2", "AI-3"]

    # Produced files landed in the shared clone after worktree merge.
    assert (clone / "scripts" / "foo.py").exists()
    assert (clone / "scripts" / "bar.py").exists()
    assert (clone / "tests" / "test_foo.py").exists()


@_SKIP_ENGINE
def test_entry_e2e_default_unset_routes_to_host(tmp_path):
    """M8c: an UNSET KAIZEN_TRANSPORT now defaults to host, so the entry runs the
    host cycle end-to-end (it no longer rejects with NotImplementedError). Same
    success shape as the explicit-host path."""
    clone = _git_init_clone(tmp_path)
    items = _native_dag()
    dag = _write_dag(tmp_path, items)
    runner = _PhaseAwareHostFakeRunner(
        impl_writes={
            "AI-1": ["scripts/foo.py"],
            "AI-2": ["scripts/bar.py"],
            "AI-3": ["tests/test_foo.py"],
        },
        notes_by_prefix=_CLEAN_REVIEW_NOTES,
    )

    out = run_host_cycle(
        action_items_file=dag,
        clone_dir=clone,
        subject="default subject",
        roster=_E2E_ROSTER,
        pm=None,
        cycle_n=1,
        run_id=None,
        test_command="true",
        env={},  # UNSET → host (the M8c default)
        runner=runner,
    )

    assert out["status"] == "success", out
    assert re.match(r"^[0-9a-f]{40}$", out["commit_sha"]), f"not a real sha: {out['commit_sha']!r}"
    assert out["participants"] == _E2E_ROSTER


@_SKIP_ENGINE
def test_entry_e2e_run_id_slug(tmp_path):
    """With --run-id, the Memex slug is kaizen:cycle:<run_id>-<cycle_n>."""
    clone = _git_init_clone(tmp_path)
    items = [
        {
            "id": "AI-1",
            "touches": ["scripts/foo.py"],
            "reads": [],
            "depends_on": [],
            "wave": 1,
            "owner": "backend-engineer-1",
        }
    ]
    dag = _write_dag(tmp_path, items)
    runner = _PhaseAwareHostFakeRunner(
        impl_writes={"AI-1": ["scripts/foo.py"]},
        notes_by_prefix=_CLEAN_REVIEW_NOTES,
    )

    out = run_host_cycle(
        action_items_file=dag,
        clone_dir=clone,
        subject="entry subject",
        roster=_E2E_ROSTER,
        pm=None,
        cycle_n=2,
        run_id=7,
        test_command="true",
        env=_HOST_ENV,
        runner=runner,
    )
    assert out["status"] == "success", out
    assert out["minutes_memex_slug"] == "kaizen:cycle:7-2"


# ── the argv main() path ─────────────────────────────────────────────────────


def test_main_guard_rejection_exits_2(tmp_path, capsys, monkeypatch):
    """main() returns 2 + a stderr message when the transport guard rejects
    (explicit KAIZEN_TRANSPORT=bridge → NotImplementedError), and prints NOTHING on
    stdout (so a caller never mistakes a guard error for an outcome dict).

    M8c: unset now defaults to host, so we set bridge EXPLICITLY to exercise the
    guard-rejection-exits-2 path."""
    monkeypatch.setenv("KAIZEN_TRANSPORT", "bridge")
    dag = _write_dag(tmp_path, _native_dag())
    rc = main(
        [
            "--action-items-file",
            str(dag),
            "--clone-dir",
            str(tmp_path),
            "--subject",
            "x",
            "--roster",
            "backend-engineer-1",
        ]
    )
    assert rc == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "host_cycle_entry" in captured.err


def test_main_shape_error_exits_2(tmp_path, capsys, monkeypatch):
    """main() returns 2 on an engine-shaped DAG, even under KAIZEN_TRANSPORT=host."""
    monkeypatch.setenv("KAIZEN_TRANSPORT", "host")
    engine_shaped = [{"task_id": "T-1", "parallel_group": 0, "writes": [], "phase": "x"}]
    dag = _write_dag(tmp_path, engine_shaped)
    rc = main(
        [
            "--action-items-file",
            str(dag),
            "--clone-dir",
            str(tmp_path),
        ]
    )
    assert rc == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "engine-shaped" in captured.err


def test_main_malformed_native_dag_missing_wave_exits_2(tmp_path, capsys, monkeypatch):
    """A native-LOOKING but malformed DAG (item missing the required `wave` key)
    flows past _assert_native_shape and trips run_host_cycle's controlled pre-validate
    (validate_dag's _check_item_shape ValueError → ActionItemsShapeError). main() must
    map it to the SAME clean `host_cycle_entry: <msg>` stderr line + exit 2 (F3), NOT a
    raw exit-1 traceback. KAIZEN_TRANSPORT=host so the guard passes and we reach the
    DAG load (no engine needed — the shape error fires before any engine call)."""
    monkeypatch.setenv("KAIZEN_TRANSPORT", "host")
    clone = _git_init_clone(tmp_path)
    malformed = [{"id": "AI-1", "touches": ["scripts/foo.py"], "reads": [], "depends_on": []}]
    dag = _write_dag(tmp_path, malformed)
    rc = main(["--action-items-file", str(dag), "--clone-dir", str(clone)])
    assert rc == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("host_cycle_entry:")
    assert "wave" in captured.err  # the error names the missing key


def test_main_malformed_native_dag_wrong_type_touches_exits_2(tmp_path, capsys, monkeypatch):
    """A native-looking DAG whose `touches` is a str (not list[str]) trips the
    pre-validate the same way → clean exit 2, not a traceback (F3)."""
    monkeypatch.setenv("KAIZEN_TRANSPORT", "host")
    clone = _git_init_clone(tmp_path)
    malformed = [
        {
            "id": "AI-1",
            "touches": "scripts/foo.py",  # str, not list[str]
            "reads": [],
            "depends_on": [],
            "wave": 1,
        }
    ]
    dag = _write_dag(tmp_path, malformed)
    rc = main(["--action-items-file", str(dag), "--clone-dir", str(clone)])
    assert rc == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("host_cycle_entry:")


def test_internal_valueerror_is_not_caught_as_operator_bug(tmp_path, monkeypatch):
    """OVER-CATCH guard: a deliberate fail-loud ValueError raised INSIDE
    host_cycle_executor (e.g. model_for on an unknown phase, _severity_rank, Finding)
    is a kaizen WIRING bug — it MUST propagate (loud crash), NOT be re-framed to the
    operator as a clean exit-2 "fix your input". With a SHAPE-VALID DAG, run_host_cycle
    passes the pre-validate and calls the executor; the executor's internal ValueError
    must escape run_host_cycle unchanged.

    Neuter-check: with main()'s OLD broad `except (..., ValueError)`, the
    main()-level assertion below goes RED (the internal ValueError is swallowed to
    rc=2). With the narrowed catch + controlled pre-validate, it propagates."""
    monkeypatch.setenv("KAIZEN_TRANSPORT", "host")
    clone = _git_init_clone(tmp_path)
    items = _native_dag()  # SHAPE-VALID → passes the pre-validate, reaches the executor
    dag = _write_dag(tmp_path, items)

    import scripts.host_executor as host_exec_mod

    sentinel_msg = "no model policy for phase 'implementation' (simulated wiring bug)"

    def _boom(**kwargs):
        raise ValueError(sentinel_msg)

    # Patch the executor on its SOURCE module — run_host_cycle does a local
    # `from scripts.host_executor import host_cycle_executor`, which resolves the
    # attribute at call time.
    monkeypatch.setattr(host_exec_mod, "host_cycle_executor", _boom)

    # (1) run_host_cycle must NOT catch it — the internal ValueError propagates.
    with pytest.raises(ValueError, match="simulated wiring bug"):
        run_host_cycle(
            action_items_file=dag,
            clone_dir=clone,
            subject="x",
            roster=_E2E_ROSTER,
            pm=None,
            cycle_n=1,
            run_id=None,
            test_command="true",
            env=_HOST_ENV,
        )

    # (2) main() must NOT re-frame it as a clean exit-2 operator bug — it propagates.
    with pytest.raises(ValueError, match="simulated wiring bug"):
        main(["--action-items-file", str(dag), "--clone-dir", str(clone), "--roster", *_E2E_ROSTER])


def test_main_serialises_outcome_to_json_stdout_and_maps_argv(tmp_path, capsys, monkeypatch):
    """main()'s argparse layer maps argv 1:1 to run_host_cycle's kwargs, AND main()
    serialises the returned outcome dict to stdout + exits 0.

    Stubs run_host_cycle (no engine) but CAPTURES its kwargs so the argv→kwarg
    mapping (host_cycle_entry.main) is actually exercised — the production caller per
    internal/cycle/SKILL.md host-path step 2. Distinguishable values are chosen so
    each of the 3 mapping mutations the review flagged goes RED:
      * cycle_n↔run_id swap (3 vs 9 — distinct, not equal),
      * a hardcoded test_command (the asserted value is a non-default sentinel),
      * a dropped --roster (the asserted roster is non-empty + ordered)."""
    monkeypatch.setenv("KAIZEN_TRANSPORT", "host")
    dag = _write_dag(tmp_path, _native_dag())

    fake_outcome = {
        "status": "success",
        "subject": "entry subject",
        "commit_sha": "a" * 40,
        "minutes_memex_slug": "kaizen:cycle:9-3",
        "participants": ["backend-engineer-1", "sdet-1"],
    }

    import scripts.host_cycle_entry as entry_mod

    captured_kwargs: dict = {}

    def _fake_run_host_cycle(**kwargs):
        captured_kwargs.update(kwargs)
        return fake_outcome

    monkeypatch.setattr(entry_mod, "run_host_cycle", _fake_run_host_cycle)
    rc = entry_mod.main(
        [
            "--action-items-file",
            str(dag),
            "--clone-dir",
            str(tmp_path),
            "--subject",
            "entry subject",
            "--roster",
            "backend-engineer-1",
            "sdet-1",
            "--pm",
            "backend-engineer-1",
            "--cycle-n",
            "3",
            "--run-id",
            "9",
            "--test-command",
            "tox -e py312",  # non-default sentinel — a hardcoded 'pytest' would mismatch
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == fake_outcome

    # The argv→kwarg mapping is 1:1. cycle_n (3) != run_id (9) so a swap is RED;
    # test_command carries the sentinel (not the 'pytest' default) so a hardcode is
    # RED; roster is the ordered non-empty list so a drop/empty is RED.
    assert captured_kwargs["action_items_file"] == str(dag)
    assert captured_kwargs["clone_dir"] == str(tmp_path)
    assert captured_kwargs["subject"] == "entry subject"
    assert captured_kwargs["roster"] == ["backend-engineer-1", "sdet-1"]
    assert captured_kwargs["pm"] == "backend-engineer-1"
    assert captured_kwargs["cycle_n"] == 3
    assert captured_kwargs["run_id"] == 9
    assert captured_kwargs["test_command"] == "tox -e py312"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
