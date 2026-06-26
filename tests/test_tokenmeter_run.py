"""Tests for the tokenmeter dynamic runner + the before/after harness (Cycle-2).

Every test injects a FAKE runner (matching the host executor's ``FakeCliRunner``
shape: ``async __call__(argv, cwd)``) that returns canned result objects AND writes
canned transcripts into ``$CLAUDE_CONFIG_DIR`` — so **no real ``claude`` is ever
spawned**. The fake's per-run ``total_cost_usd`` is computed to AGREE with the
transcript's priced cost, so the Seam-A/Seam-B reconciliation lands ``agree``.

Coverage:
  * ``N=3`` → the report's dynamic figures carry ``n=3`` + a non-None ``cv``.
  * run / phase / cycle tagging is LIVE — records carry all three and the
    renderer's ``aggregate`` groups by each (resolves Cycle-1's residue).
  * ``benchmark_target`` combines the real static footprint + the dynamic aggregate.
  * a full before → after → ``report`` delta on two real reports, and the
    control-vector gate FIRES on drift.

Stdlib + pytest only. Transcript / result content is DATA.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.tokenmeter import main
from scripts.tokenmeter_pricing import cost_usd
from scripts.tokenmeter_render import aggregate
from scripts.tokenmeter_run import (
    _harvest,
    benchmark_target,
    default_phase_resolver,
    run_scenario,
)
from scripts.tokenmeter_scenario import Scenario
from scripts.tokenmeter_schema import ControlDriftError, assert_controls_match
from scripts.tokenmeter_transcript import collect_usage_records

REPO_ROOT = Path(__file__).resolve().parent.parent
MODEL = "claude-haiku-4-5"  # base input rate 1.0 → simple, non-zero cost


@pytest.fixture(autouse=True)
def shared_config_root(monkeypatch, tmp_path_factory):
    """Point ``$CLAUDE_CONFIG_DIR`` at a fresh, ISOLATED shared root for every test.

    The fix runs ``claude`` against the AMBIENT config (auth-preserving — no per-run
    relocation), so the fake writes its canned transcripts here and the harvest reads
    them back from here. A unique dir per test keeps the developer's real ``~/.claude``
    untouched and the runs hermetic.
    """
    root = tmp_path_factory.mktemp("claude-config")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    return root


# ── inline transcript + fake runner ──────────────────────────────────────────


def _usage(inp, out, *, cache_read=0, cache_write=0):
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_creation_input_tokens": cache_write,
        "cache_read_input_tokens": cache_read,
    }


def _assistant_line(message_id, usage, *, session_id="sess-main", is_sidechain=False, model=MODEL):
    obj = {
        "type": "assistant",
        "sessionId": session_id,
        "timestamp": "2026-06-25T12:00:00Z",
        "message": {"id": message_id, "model": model, "usage": usage},
    }
    if is_sidechain:
        obj["isSidechain"] = True
    return json.dumps(obj)


def _run_cost(orch_usage, sub_usage):
    """Priced cost of one run's two transcript lines — so the oracle AGREES."""
    total = cost_usd(orch_usage, MODEL).total_cost
    if sub_usage is not None:
        total += cost_usd(sub_usage, MODEL).total_cost
    return total


class FakeBenchmarkRunner:
    """Injectable runner: returns canned result objects + writes canned transcripts.

    Matches the FakeCliRunner shape. Per run i it writes — into the SHARED
    ``$CLAUDE_CONFIG_DIR`` (which the auth-preserving harness does NOT relocate) — an
    orchestrator assistant line and (by default) a reparented sidechain sub-agent
    line, BOTH carrying that run's distinct ``session_id`` (``sess-<i>``), and returns
    a result envelope carrying the same ``session_id`` (so the harness scopes the
    harvest by it). When ``with_unrelated`` is set it ALSO writes an UNRELATED session
    (``sess-unrelated``) under the same root, to prove the session_id scope EXCLUDES
    it. The per-call config dir actually seen is recorded so a test can assert the
    harness never relocated it. NO real process.
    """

    no_real_process = True
    is_fake = True

    def __init__(
        self,
        runs,
        *,
        agent_type="backend-engineer-1",
        with_sidechain=True,
        with_unrelated=False,
        unrelated_usage=None,
        unrelated_session="sess-unrelated",
    ):
        # runs: list of (orch_usage, sub_usage_or_None) per run.
        self._runs = runs
        self._agent_type = agent_type
        self._with_sidechain = with_sidechain
        self._with_unrelated = with_unrelated
        self._unrelated_usage = unrelated_usage or _usage(9999, 8888, cache_read=7777)
        self._unrelated_session = unrelated_session
        self._i = 0
        self.calls: list[dict] = []

    async def __call__(self, argv, cwd):
        cfg = Path(os.environ["CLAUDE_CONFIG_DIR"])  # the dir the harness pointed claude at
        # Record the config dir actually seen — a test asserts it is the ambient
        # (shared) root on every run (no per-run relocation), or the seeded override.
        self.calls.append({"argv": list(argv), "cwd": cwd, "config_dir": str(cfg)})
        orch, sub = self._runs[self._i]
        self._i += 1
        session_id = f"sess-{self._i}"  # each run gets its OWN session (as real claude does)

        proj = cfg / "projects" / "proj"
        sess = proj / session_id
        sess.mkdir(parents=True, exist_ok=True)
        (sess / "session.jsonl").write_text(
            _assistant_line(f"orch-{self._i}", orch, session_id=session_id) + "\n",
            encoding="utf-8",
        )
        result_usage = dict(orch)
        if self._with_sidechain and sub is not None:
            subs = sess / "subagents"
            subs.mkdir(parents=True, exist_ok=True)
            # The subagent line is REPARENTED to the parent session (its own sessionId
            # IS the parent uuid), so the parent's session_id scope captures it too.
            (subs / "agent-x.jsonl").write_text(
                _assistant_line(f"sub-{self._i}", sub, session_id=session_id, is_sidechain=True)
                + "\n",
                encoding="utf-8",
            )
            (subs / "agent-x.meta.json").write_text(
                json.dumps({"agentType": self._agent_type}), encoding="utf-8"
            )
            for k in result_usage:
                result_usage[k] += sub[k]

        if self._with_unrelated:
            # A DIFFERENT, concurrent session sharing the config root — its records
            # MUST be excluded by the run's session_id scope.
            unrel = proj / self._unrelated_session
            unrel.mkdir(parents=True, exist_ok=True)
            (unrel / "session.jsonl").write_text(
                _assistant_line(
                    "unrelated-1", self._unrelated_usage, session_id=self._unrelated_session
                )
                + "\n",
                encoding="utf-8",
            )

        return json.dumps(
            {
                "total_cost_usd": _run_cost(orch, sub if self._with_sidechain else None),
                "usage": result_usage,
                "is_error": False,
                "session_id": session_id,
                "num_turns": 1,
                "stop_reason": "end_turn",
            }
        )


def _scenario(target="skills/improve"):
    return Scenario.create(
        name="bench",
        target=target,
        prompt="exercise the target on a representative task",
        source="user",
        cycles=1,
        subject="leaner target",
    )


def _three_varied_runs():
    """Three runs with VARYING tokens so the per-run CV is positive (non-None)."""
    return [
        (_usage(100, 50, cache_read=1000), _usage(20, 80, cache_read=500)),
        (_usage(120, 60, cache_read=1100), _usage(25, 90, cache_read=520)),
        (_usage(110, 55, cache_read=1050), _usage(22, 85, cache_read=510)),
    ]


# ── N=3 → n=3 + non-None cv ──────────────────────────────────────────────────


def test_run_scenario_n3_populates_n_and_cv():
    runner = FakeBenchmarkRunner(_three_varied_runs())
    report = run_scenario(
        _scenario(),
        n=3,
        runner=runner,
        metadata={"model": MODEL, "transport": "host", "effort": "high"},
    )
    assert len(runner.calls) == 3
    # The headless argv shape the harness builds.
    argv = runner.calls[0]["argv"]
    assert argv[:2] == ["claude", "-p"]
    assert argv[2] == _scenario().prompt
    assert argv[3:5] == ["--output-format", "json"]
    # Major-2(a): --model + --permission-mode are forwarded to claude.
    assert argv[argv.index("--model") + 1] == MODEL
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"

    cat = {row["row"]: row["after"] for row in report["rows"] if row["kind"] == "category"}
    assert cat["input_tokens"]["n"] == 3
    assert cat["output_tokens"]["n"] == 3
    # Tokens varied across runs → cv is a positive, non-None float.
    assert cat["input_tokens"]["cv"] is not None
    assert cat["input_tokens"]["cv"] > 0
    assert report["metadata"]["n_runs"] == 3
    # The scenario's stable hash flows into the comparability control vector.
    assert report["metadata"]["scenario_hash"] == _scenario().scenario_hash


def test_run_scenario_oracle_reconciles_agree():
    runner = FakeBenchmarkRunner(_three_varied_runs())
    report = run_scenario(_scenario(), n=3, runner=runner, metadata={"model": MODEL})
    # Fake oracle cost == priced transcript cost → reconciliation AGREES.
    assert report["cost_oracle"]["reconciled"] == "agree"
    assert report["cost_oracle"]["seam_a_total_cost_usd"] is not None


def test_missing_runner_raises():
    with pytest.raises(ValueError):
        run_scenario(_scenario(), n=3, runner=None)


# ── run / phase / cycle tagging is LIVE (Cycle-1 residue resolved) ───────────


def test_harvest_tags_run_phase_cycle_and_aggregate_groups_by_each():
    runner = FakeBenchmarkRunner(_three_varied_runs())
    records, oracle, statuses = _harvest(
        _scenario(),
        n=3,
        runner=runner,
        cwd=None,
        config_dir=None,
        phase_resolver=None,
        cycle="c1",
    )
    assert len(statuses) == 3
    assert oracle is not None

    # every record carries a run, a phase, and the cycle
    assert {r.run for r in records} == {"1", "2", "3"}
    assert all(r.cycle == "c1" for r in records)
    assert {r.phase for r in records} == {"orchestrate", "implement"}

    # the renderer's aggregate groups by each axis (GROUP_KEYS handles cycle)
    by_run = aggregate(records, ["run"])
    assert {row["run"] for row in by_run} == {"1", "2", "3"}
    by_phase = aggregate(records, ["phase"])
    assert {row["phase"] for row in by_phase} == {"orchestrate", "implement"}
    by_cycle = aggregate(records, ["cycle"])
    assert len(by_cycle) == 1 and by_cycle[0]["cycle"] == "c1"
    # four categories stay split — never one summed total.
    assert set(by_cycle[0]) >= {
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    }


def test_per_phase_rows_non_empty_on_report():
    runner = FakeBenchmarkRunner(_three_varied_runs())
    report = run_scenario(_scenario(), n=3, runner=runner, cycle="c1", metadata={"model": MODEL})
    phases = {row["row"] for row in report["rows"] if row["kind"] == "phase"}
    roles = {row["row"] for row in report["rows"] if row["kind"] == "role"}
    assert phases == {"orchestrate", "implement"}
    assert roles == {"backend-engineer-1"}
    # per-phase figures are themselves N-run aggregates (CV live)
    phase_rows = {row["row"]: row["after"] for row in report["rows"] if row["kind"] == "phase"}
    assert phase_rows["implement"]["n"] == 3


def test_default_phase_resolver_orchestrator_vs_subagent():
    orch = collect_one_record_orchestrator()
    assert default_phase_resolver(orch) == "orchestrate"


def collect_one_record_orchestrator():
    from scripts.tokenmeter_model import TokenUsage, UsageRecord

    return UsageRecord(usage=TokenUsage(input_tokens=1), is_sidechain=False)


def test_default_phase_resolver_maps_roles():
    from scripts.tokenmeter_model import TokenUsage, UsageRecord

    def rec(label):
        return UsageRecord(usage=TokenUsage(), is_sidechain=True, agent_label=label)

    assert default_phase_resolver(rec("software-architect-1")) == "design"
    assert default_phase_resolver(rec("sdet-1")) == "review"
    assert default_phase_resolver(rec("technical-writer-1")) == "pr"
    assert default_phase_resolver(rec("backend-engineer-1")) == "implement"
    assert default_phase_resolver(rec("unknown-role")) == "implement"


# ── benchmark_target combines static + dynamic ──────────────────────────────


def test_benchmark_target_combines_static_and_dynamic():
    runner = FakeBenchmarkRunner(_three_varied_runs())
    report = benchmark_target(
        _scenario("skills/improve"),
        n=3,
        runner=runner,
        repo_root=REPO_ROOT,
        metadata={"model": MODEL, "transport": "host", "effort": "high"},
    )
    kinds = {row["kind"] for row in report["rows"]}
    # dynamic category rows AND static overhead rows in ONE report
    assert "category" in kinds
    assert "overhead" in kinds
    overhead = [row for row in report["rows"] if row["kind"] == "overhead"]
    # the real skills/improve SKILL.md footprint is present and static-sourced
    assert any("skills/improve/SKILL.md" in str(row["row"]) for row in overhead)
    assert all(row["mode"] == "static" for row in overhead)
    cat = [row for row in report["rows"] if row["kind"] == "category"]
    assert all(row["mode"] == "dynamic" for row in cat)


# ── AUTH FIX: no CLAUDE_CONFIG_DIR relocation by default; scope by session_id ──


def test_no_config_dir_relocation_by_default(shared_config_root):
    """THE FIX (default path): the harness runs ``claude`` against the AMBIENT
    ``$CLAUDE_CONFIG_DIR`` on every run — it never relocates it to a fresh per-run
    dir (which would point claude at an empty dir and break ``Not logged in``
    subscription auth). The runner therefore sees the same ambient root every call,
    and the env is intact afterwards."""
    runner = FakeBenchmarkRunner(_three_varied_runs())
    run_scenario(_scenario(), n=3, runner=runner, metadata={"model": MODEL})
    # Every run saw the ambient root — NOT a relocated per-run config dir.
    assert [c["config_dir"] for c in runner.calls] == [str(shared_config_root)] * 3
    assert os.environ["CLAUDE_CONFIG_DIR"] == str(shared_config_root)


def test_config_dir_override_relocates_during_run_and_restores(shared_config_root, tmp_path):
    """The OPTIONAL ``config_dir=`` override (for callers who HAVE seeded credentials)
    still works: it is applied for the duration of each run and the ambient value is
    restored afterwards (no env leak)."""
    seeded = tmp_path / "seeded-creds"
    seeded.mkdir()
    runner = FakeBenchmarkRunner(_three_varied_runs())
    report = run_scenario(
        _scenario(), n=2, runner=runner, config_dir=str(seeded), metadata={"model": MODEL}
    )
    # Override applied for both runs...
    assert [c["config_dir"] for c in runner.calls] == [str(seeded)] * 2
    # ...records harvested from the seeded dir (so the report is non-empty)...
    assert any(row["kind"] == "category" for row in report["rows"])
    # ...and the ambient value restored afterwards.
    assert os.environ["CLAUDE_CONFIG_DIR"] == str(shared_config_root)


def test_run_scenario_scopes_by_session_id_excludes_unrelated(tmp_path):
    """THE FIX (session scope): with an orchestrator record + a reparented subagent
    record (both session=S) AND an UNRELATED session (session=X) under the SHARED
    config root, ``run_scenario`` harvests ONLY S's two records — never X. Asserted on
    the emitted report's per-category totals AND the per-call evidence."""
    runs = [(_usage(100, 50, cache_read=1000), _usage(20, 80, cache_read=500))]
    runner = FakeBenchmarkRunner(
        runs, with_unrelated=True, unrelated_usage=_usage(9999, 8888, cache_read=7777)
    )
    evidence = tmp_path / "evidence.jsonl"
    report = run_scenario(
        _scenario(), n=1, runner=runner, metadata={"model": MODEL}, evidence_out=str(evidence)
    )
    cat = {row["row"]: row["after"] for row in report["rows"] if row["kind"] == "category"}
    # ONLY session S's two records (orchestrator + reparented subagent) are counted;
    # the unrelated session's huge counts never leak into the totals.
    assert cat["input_tokens"]["n"] == 1
    assert cat["input_tokens"]["mean"] == 120  # 100 + 20, NOT + 9999
    assert cat["output_tokens"]["mean"] == 130  # 50 + 80, NOT + 8888
    assert cat["cache_read_input_tokens"]["mean"] == 1500  # 1000 + 500, NOT + 7777
    # The per-call evidence carries exactly the two in-session records.
    lines = [json.loads(ln) for ln in evidence.read_text().splitlines() if ln.strip()]
    assert len(lines) == 2
    assert {ln["session_id"] for ln in lines} == {"sess-1"}
    assert all(ln["session_id"] != "sess-unrelated" for ln in lines)


def test_window_fallback_scopes_by_time_when_no_session_id(caplog):
    """Fallback: when the result envelope carries NO session_id, the harness scopes
    the harvest by a time window around the run (logging that the scope is APPROXIMATE).
    An in-window record is kept; an out-of-window (year-2000) record is excluded."""

    class NoSessionIdRunner:
        is_fake = True

        def __init__(self):
            self.calls: list[dict] = []

        async def __call__(self, argv, cwd):
            cfg = Path(os.environ["CLAUDE_CONFIG_DIR"])
            self.calls.append({"argv": list(argv)})
            now_iso = datetime.now(UTC).isoformat()
            now_sess = cfg / "projects" / "proj" / "sess-now"
            now_sess.mkdir(parents=True, exist_ok=True)
            (now_sess / "session.jsonl").write_text(
                json.dumps(
                    {
                        "type": "assistant",
                        "sessionId": "sess-now",
                        "timestamp": now_iso,  # within the run window
                        "message": {"id": "orch-now", "model": MODEL, "usage": _usage(40, 20)},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            old_sess = cfg / "projects" / "proj" / "sess-old"
            old_sess.mkdir(parents=True, exist_ok=True)
            (old_sess / "session.jsonl").write_text(
                json.dumps(
                    {
                        "type": "assistant",
                        "sessionId": "sess-old",
                        "timestamp": "2000-01-01T00:00:00Z",  # far outside the window
                        "message": {"id": "orch-old", "model": MODEL, "usage": _usage(9999, 9999)},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            # Deliberately OMIT session_id from the envelope → forces the time-window fallback.
            return json.dumps(
                {
                    "total_cost_usd": _run_cost(_usage(40, 20), None),
                    "usage": _usage(40, 20),
                    "is_error": False,
                    "num_turns": 1,
                }
            )

    runner = NoSessionIdRunner()
    with caplog.at_level(logging.WARNING, logger="scripts.tokenmeter_run"):
        report = run_scenario(_scenario(), n=1, runner=runner, metadata={"model": MODEL})
    cat = {row["row"]: row["after"] for row in report["rows"] if row["kind"] == "category"}
    # Only the in-window record contributes; the year-2000 record is excluded.
    assert cat["input_tokens"]["mean"] == 40  # NOT 40 + 9999
    assert cat["output_tokens"]["mean"] == 20  # NOT 20 + 9999
    assert "APPROXIMATE" in caplog.text  # the fallback logged that the scope is approximate


# ── full before → after → report delta + control-vector gate ─────────────────


def _emit(
    tmp_path,
    name,
    *,
    model=MODEL,
    target_commit="abc123",
    runner_runs=None,
    transport="host",
    effort="high",
    cycles=None,
    prompt="exercise the target on a representative task",
):
    """Drive the CLI `benchmark` subcommand with an injected fake runner → a file.

    The control-vector inputs (``model`` / ``transport`` / ``effort`` / ``cycles``
    and the ``prompt`` that drives ``scenario_hash``) are all overridable so a
    drift test can perturb exactly one control between a before/after pair.
    """
    out = tmp_path / name
    scn = tmp_path / f"scn-{name}.json"
    scn.write_text(
        json.dumps(
            {
                "name": "bench",
                "target": "skills/improve",
                "prompt": prompt,
                "source": "user",
                "cycles": 1,
                "subject": "leaner target",
            }
        ),
        encoding="utf-8",
    )
    argv = [
        "benchmark",
        str(scn),
        "--model",
        model,
        "--transport",
        transport,
        "--effort",
        effort,
        "--n",
        "3",
        "--repo-root",
        str(REPO_ROOT),
        "--target-commit",
        target_commit,
        "--out",
        str(out),
    ]
    if cycles is not None:
        argv += ["--cycles", str(cycles)]
    runner = FakeBenchmarkRunner(runner_runs or _three_varied_runs())
    rc = main(argv, runner=runner)
    assert rc == 0
    return out


def test_full_before_after_report_flow(tmp_path, capsys):
    # BEFORE: baseline on the (notionally) unmodified target.
    before = _emit(tmp_path, "before.json", target_commit="before-sha")
    capsys.readouterr()
    # AFTER: post measurement; the improved target ran LEANER (fewer tokens).
    leaner = [
        (_usage(60, 30, cache_read=600), _usage(10, 40, cache_read=300)),
        (_usage(70, 35, cache_read=650), _usage(12, 45, cache_read=320)),
        (_usage(65, 32, cache_read=620), _usage(11, 42, cache_read=310)),
    ]
    after = _emit(tmp_path, "after.json", target_commit="after-sha", runner_runs=leaner)
    capsys.readouterr()

    # Both reports are real, well-formed JSON with the SAME control vector.
    before_doc = json.loads(before.read_text())
    after_doc = json.loads(after.read_text())
    assert_controls_match(before_doc["metadata"], after_doc["metadata"])  # no raise
    # target_commit is NOT a control — it is expected to differ.
    assert before_doc["metadata"]["target_commit"] != after_doc["metadata"]["target_commit"]

    # The EXISTING `report` subcommand renders the delta.
    rc = main(["report", str(before), str(after), "--format", "json"])
    assert rc == 0
    delta = json.loads(capsys.readouterr().out)
    cat = {row["row"]: row for row in delta["rows"] if row["kind"] == "category"}
    # input tokens dropped → negative delta (a genuine token win, attributable).
    assert cat["input_tokens"]["delta_abs"] < 0
    assert cat["input_tokens"]["before"] is not None
    assert cat["input_tokens"]["after"] is not None


def test_report_refuses_delta_on_control_drift(tmp_path, capsys):
    """The control-vector gate FIRES when a control (model) drifts — the delta is
    refused so a non-comparable before/after can never be reported as a 'win'."""
    before = _emit(tmp_path, "before.json", model="claude-haiku-4-5")
    capsys.readouterr()
    after = _emit(tmp_path, "after.json", model="claude-opus-4-7")  # model DRIFTED
    capsys.readouterr()

    # main() catches ControlDriftError → prints an error line + returns 1.
    rc = main(["report", str(before), str(after)])
    out = capsys.readouterr().out
    assert rc == 1
    assert json.loads(out)["status"] == "error"

    # and the underlying gate raises directly.
    with pytest.raises(ControlDriftError):
        assert_controls_match(
            json.loads(before.read_text())["metadata"],
            json.loads(after.read_text())["metadata"],
        )


@pytest.mark.parametrize(
    "drifted, before_kw, after_kw",
    [
        ("transport", {"transport": "host"}, {"transport": "bridge"}),
        ("cycles", {"cycles": 1}, {"cycles": 2}),
        ("effort", {"effort": "high"}, {"effort": "low"}),
        # A different prompt → a different scenario_hash (the comparability key).
        (
            "scenario_hash",
            {"prompt": "scenario prompt variant A"},
            {"prompt": "scenario prompt variant B"},
        ),
    ],
)
def test_report_refuses_delta_on_each_control_drift(tmp_path, capsys, drifted, before_kw, after_kw):
    """NIT-d: the control gate must fire on EACH control, not just `model`. Drift one
    control at a time (transport / cycles / effort / scenario_hash) and assert the
    `report` delta is refused AND the underlying gate raises naming the drifted key."""
    before = _emit(tmp_path, "before.json", **before_kw)
    capsys.readouterr()
    after = _emit(tmp_path, "after.json", **after_kw)
    capsys.readouterr()

    rc = main(["report", str(before), str(after)])
    out = capsys.readouterr().out
    assert rc == 1
    assert json.loads(out)["status"] == "error"

    before_md = json.loads(before.read_text())["metadata"]
    after_md = json.loads(after.read_text())["metadata"]
    # Exactly the intended control drifted (others held constant).
    assert before_md[drifted] != after_md[drifted]
    with pytest.raises(ControlDriftError):
        assert_controls_match(before_md, after_md)


# ── failed run is not a $0 success ───────────────────────────────────────────


def test_failed_run_classified_as_failure(tmp_path):
    """A runner that returns an empty blob (and writes no transcript) yields a
    FAILURE status, never a $0 success."""

    class EmptyRunner:
        is_fake = True

        async def __call__(self, argv, cwd):
            return ""  # 0-byte result → parse_result raises → classify FAILURE

    from scripts.tokenmeter_result import RunStatus

    records, oracle, statuses = _harvest(
        _scenario(),
        n=2,
        runner=EmptyRunner(),
        cwd=None,
        config_dir=None,
        phase_resolver=None,
        cycle=None,
    )
    assert statuses == [RunStatus.FAILURE, RunStatus.FAILURE]
    assert records == []
    assert oracle is None


# ── MAJOR-1: an all-FAILURE harvest must NOT emit a report that reads clean ───


class AllFailRunner:
    """Injectable runner whose every call FAILS (is_error / no transcript)."""

    is_fake = True

    def __init__(self):
        self.calls: list[dict] = []

    async def __call__(self, argv, cwd):
        self.calls.append({"argv": list(argv), "cwd": cwd})
        # is_error=True → classify_result maps to FAILURE; no transcript written.
        return json.dumps(
            {
                "is_error": True,
                "total_cost_usd": 0,
                "usage": _usage(0, 0),
                "session_id": "sess-main",
            }
        )


def _write_scenario_file(tmp_path, *, target="skills/improve"):
    scn = tmp_path / "scn.json"
    scn.write_text(
        json.dumps(
            {
                "name": "bench",
                "target": target,
                "prompt": "exercise the target on a representative task",
                "source": "user",
                "cycles": 1,
                "subject": "leaner target",
            }
        ),
        encoding="utf-8",
    )
    return scn


def test_all_failure_run_exposes_failures_in_emitted_report():
    """The EMITTED report (public api), not _harvest's return, must expose that every
    run FAILED — otherwise an all-FAILURE run reads like a clean no-op (empty category
    rows, reconciled=unreconciled, n_runs=3, no failure marker)."""
    runner = AllFailRunner()
    report = run_scenario(_scenario(), n=3, runner=runner, metadata={"model": MODEL})

    runs = report["runs"]
    assert runs["n_runs"] == 3
    assert runs["runs_failed"] == 3
    assert runs["any_failed"] is True
    assert runs["all_failed"] is True
    assert runs["statuses"] == ["failure", "failure", "failure"]
    # The masking trap: metadata still says n_runs=3 and the category rows are empty,
    # but the runs block is the explicit fail-loud marker that it was NOT a clean run.
    assert report["metadata"]["n_runs"] == 3
    assert [r for r in report["rows"] if r["kind"] == "category"] == []


def test_benchmark_target_all_failure_marks_runs_block():
    """Same fail-loud guarantee on the static+dynamic `benchmark_target` entry."""
    runner = AllFailRunner()
    report = benchmark_target(
        _scenario("skills/improve"),
        n=2,
        runner=runner,
        repo_root=REPO_ROOT,
        metadata={"model": MODEL},
    )
    assert report["runs"] == {
        "n_runs": 2,
        "statuses": ["failure", "failure"],
        "runs_failed": 2,
        "any_failed": True,
        "all_failed": True,
    }
    # static overhead rows still render (the static footprint is independent).
    assert any(r["kind"] == "overhead" for r in report["rows"])


def test_cmd_benchmark_prints_failed_runs_to_stderr(tmp_path, capsys):
    """cmd_benchmark must surface failed runs on stderr (and the report file carries
    the runs block)."""
    out = tmp_path / "before.json"
    scn = _write_scenario_file(tmp_path)
    rc = main(
        [
            "benchmark",
            str(scn),
            "--model",
            MODEL,
            "--n",
            "2",
            "--repo-root",
            str(REPO_ROOT),
            "--out",
            str(out),
        ],
        runner=AllFailRunner(),
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "FAILED" in err
    assert "2/2" in err
    report = json.loads(out.read_text())
    assert report["runs"]["any_failed"] is True


# ── MAJOR-2: real runner forwards --model/--permission-mode + env hardening ───


def test_real_runner_argv_wires_model_into_control_vector():
    """The model passed to claude via --model MUST equal report.metadata.model, so a
    before/after model drift is caught by the control-vector gate (not decorative)."""
    runner = FakeBenchmarkRunner(_three_varied_runs())
    report = run_scenario(_scenario(), n=3, runner=runner, metadata={"model": MODEL})
    argv = runner.calls[0]["argv"]
    assert argv[argv.index("--model") + 1] == MODEL
    assert report["metadata"]["model"] == MODEL
    assert report["metadata"]["model"] == argv[argv.index("--model") + 1]


def test_real_runner_omits_model_flag_when_no_model():
    """No model supplied → no decorative --model flag (and metadata.model is empty)."""
    runner = FakeBenchmarkRunner(_three_varied_runs())
    report = run_scenario(_scenario(), n=1, runner=runner, metadata={})
    assert "--model" not in runner.calls[0]["argv"]
    assert report["metadata"]["model"] == ""


def test_permission_mode_default_and_override():
    """--permission-mode defaults to the proven sibling's acceptEdits and is overridable."""
    runner = FakeBenchmarkRunner(_three_varied_runs())
    run_scenario(_scenario(), n=1, runner=runner, metadata={"model": MODEL})
    argv = runner.calls[0]["argv"]
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"

    runner2 = FakeBenchmarkRunner(_three_varied_runs())
    run_scenario(
        _scenario(), n=1, runner=runner2, metadata={"model": MODEL}, permission_mode="plan"
    )
    argv2 = runner2.calls[0]["argv"]
    assert argv2[argv2.index("--permission-mode") + 1] == "plan"


def test_build_subprocess_env_drops_secrets_keeps_config_dir():
    """Major-2(c): the real runner's env is an allowlist — secrets dropped, the per-run
    CLAUDE_CONFIG_DIR isolation + auth-bearing HOME survive."""
    from scripts.tokenmeter_run import _build_subprocess_env

    parent = {
        "ANTHROPIC_API_KEY": "sk-should-not-leak",
        "GH_TOKEN": "ghp_should_not_leak",
        "GITHUB_TOKEN": "ght_should_not_leak",
        "AWS_SECRET_ACCESS_KEY": "should-not-leak",
        "PATH": "/usr/bin",
        "HOME": "/home/u",
        "CLAUDE_CONFIG_DIR": "/tmp/run-7",
        "LC_ALL": "C.UTF-8",
    }
    env = _build_subprocess_env(parent)
    assert "ANTHROPIC_API_KEY" not in env
    assert "GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/u"
    assert env["CLAUDE_CONFIG_DIR"] == "/tmp/run-7"  # per-run isolation survives
    assert env["LC_ALL"] == "C.UTF-8"


# ── MINOR-a: CLI can populate the outcome footer (design §5) ──────────────────


def test_cmd_benchmark_threads_outcomes(tmp_path, capsys):
    out = tmp_path / "before.json"
    scn = _write_scenario_file(tmp_path)
    rc = main(
        [
            "benchmark",
            str(scn),
            "--model",
            MODEL,
            "--n",
            "3",
            "--repo-root",
            str(REPO_ROOT),
            "--cycles-succeeded",
            "2",
            "--cycles-abandoned",
            "1",
            "--pr-opened",
            "--tests-green",
            "--out",
            str(out),
        ],
        runner=FakeBenchmarkRunner(_three_varied_runs()),
    )
    assert rc == 0
    capsys.readouterr()
    report = json.loads(out.read_text())
    assert report["outcome"] == {
        "cycles_succeeded": 2,
        "cycles_abandoned": 1,
        "pr_opened": True,
        "tests_green": True,
    }


# ── MINOR-b: per-call evidence JSONL + daily rollup survive cleanup ──────────


def test_cmd_benchmark_writes_evidence_and_rollup(tmp_path, capsys):
    out = tmp_path / "before.json"
    evidence = tmp_path / "evidence.jsonl"
    rollup = tmp_path / "rollup.json"
    scn = _write_scenario_file(tmp_path)
    rc = main(
        [
            "benchmark",
            str(scn),
            "--model",
            MODEL,
            "--n",
            "3",
            "--repo-root",
            str(REPO_ROOT),
            "--evidence-out",
            str(evidence),
            "--rollup-out",
            str(rollup),
            "--out",
            str(out),
        ],
        runner=FakeBenchmarkRunner(_three_varied_runs()),
    )
    assert rc == 0
    capsys.readouterr()

    # §5 per-call JSONL evidence: one tokscale-compatible object per record
    # (2 records/run x 3 runs), with the four split categories + tokscale fields.
    lines = [ln for ln in evidence.read_text().splitlines() if ln.strip()]
    assert len(lines) == 6
    first = json.loads(lines[0])
    assert set(first) >= {
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "model",
        "timestamp",
        "session_id",
    }

    # §7 daily rollup: a tokscale-compatible per-(day, model) list, categories split.
    daily = json.loads(rollup.read_text())
    assert isinstance(daily, list) and daily
    assert set(daily[0]) >= {
        "day",
        "model",
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    }


def test_collect_usage_records_smoke_no_real_claude(shared_config_root):
    """Sanity: the harvest reads transcripts the fake wrote under the SHARED config
    root (never a real ~/.claude), and the session_id scope splits the three sessions
    cleanly into the per-run harvest."""
    runner = FakeBenchmarkRunner(_three_varied_runs())
    records, _oracle, _statuses = _harvest(
        _scenario(),
        n=3,
        runner=runner,
        cwd=None,
        config_dir=None,  # default: harvest the ambient (shared) root, scope by session_id
        phase_resolver=None,
        cycle=None,
    )
    # The fake wrote three distinct sessions under the shared root...
    assert (shared_config_root / "projects").is_dir()
    # ...an UNFILTERED collect over the whole root sees all 6 records (3 sessions x 2)...
    assert len(collect_usage_records(config_dir=shared_config_root)) == 6
    # ...and the session_id scope yields exactly those 6 across the three runs.
    assert len(records) == 6  # 2 per run x 3 runs
    assert {r.run for r in records} == {"1", "2", "3"}


def test_cmd_benchmark_config_dir_override_is_threaded(tmp_path, capsys):
    """The CLI ``--config-dir`` flag reaches the harness: the injected runner is
    invoked against the seeded dir (relocation on the override path), while the
    DEFAULT (no flag) leaves the ambient root untouched."""
    seeded = tmp_path / "seeded-creds"
    seeded.mkdir()
    out = tmp_path / "before.json"
    scn = _write_scenario_file(tmp_path)
    runner = FakeBenchmarkRunner(_three_varied_runs())
    rc = main(
        [
            "benchmark",
            str(scn),
            "--model",
            MODEL,
            "--n",
            "2",
            "--repo-root",
            str(REPO_ROOT),
            "--config-dir",
            str(seeded),
            "--out",
            str(out),
        ],
        runner=runner,
    )
    assert rc == 0
    capsys.readouterr()
    assert [c["config_dir"] for c in runner.calls] == [str(seeded)] * 2
    # The override-path runs harvested from the seeded dir → a non-empty report.
    report = json.loads(out.read_text())
    assert any(row["kind"] == "category" for row in report["rows"])


def test_run_scenario_is_synchronous_no_event_loop_required():
    """run_scenario must be callable from plain sync code (it owns asyncio.run)."""
    assert not _loop_running()
    runner = FakeBenchmarkRunner(_three_varied_runs())
    run_scenario(_scenario(), n=3, runner=runner, metadata={"model": MODEL})


def _loop_running():
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False
