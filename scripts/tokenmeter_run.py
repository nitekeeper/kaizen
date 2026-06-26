"""Token-meter dynamic runner — the before/after harness (Cycle-2).

This is the dynamic half of kaizen's token-usage benchmark: it replays a
:class:`~scripts.tokenmeter_scenario.Scenario`'s prompt through a headless
``claude -p <prompt> --output-format json`` run **N times**, harvests the real
four-category usage from BOTH seams, and folds the N runs into a single
``BenchmarkReport`` via the Cycle-1 assembly layer
(:func:`scripts.tokenmeter_schema.assemble`) — so the dynamic ``{n, mean, cv}``
aggregate is populated (``n=3`` by default) instead of a single-run scalar.

Two public entry points:

* :func:`run_scenario` — the dynamic-only report (the N-run aggregate, no static
  footprint).
* :func:`benchmark_target` — ONE report combining the static footprint of
  ``scenario.target`` (:func:`scripts.tokenmeter_static.static_footprint`) with the
  dynamic N-run aggregate. This is what the CLI ``benchmark`` subcommand emits and
  what the before/after flow writes to ``before.json`` / ``after.json``.

The ``runner`` is **INJECTABLE** and matches the host executor's ``FakeCliRunner``
shape — ``async __call__(argv, cwd)`` resolving to the raw result envelope. The
default (:func:`real_claude_runner`) is the ONLY new external call in the meter; it
shells the real ``claude`` binary via ``asyncio.create_subprocess_exec`` (no shell
string). Tests inject a fake that returns canned result objects and writes canned
transcripts, so **no real ``claude`` is ever spawned in the suite**.

AUTH-PRESERVING SESSION SCOPE (the Cycle-2 fix — kaizen run, 2026-06-26 live
smoke). An earlier shape relocated ``$CLAUDE_CONFIG_DIR`` to a fresh per-run dir to
isolate each run's transcripts. But ``claude`` reads its **subscription
credentials** from ``$CLAUDE_CONFIG_DIR``, so pointing it at an empty dir makes
every real run fail ``Not logged in`` (verified live: the SAME argv/env WITHOUT
relocation authenticates and returns real usage). The harness therefore runs
``claude`` against the **normal/default** config (no relocation by default) and
isolates each run's records by **session_id** instead — which is how the design
correlates runs anyway. An optional ``config_dir=`` override remains for callers
who HAVE seeded credentials into a dedicated dir.

Per run i the harness:

1. invokes the runner with ``["claude", "-p", prompt, "--output-format", "json",
   "--model", <model>, "--permission-mode", <mode>]`` (``--model`` only when a
   model is supplied — the SAME string that seeds ``metadata.model``, so the
   control vector is not decorative), against the AMBIENT ``$CLAUDE_CONFIG_DIR``
   (so subscription auth works) — or, when a ``config_dir=`` override is supplied,
   against that dir;
2. parses the Seam-A result object (cost oracle, summed across runs for
   reconciliation; ``session_id`` extracted here) and classifies the run (a failed
   run is not a $0 success);
3. harvests the Seam-B transcripts and FILTERS them to that run's ``session_id``.
   Sidechain/subagent lines are reparented to the parent ``session_id`` in Seam B
   (empirically confirmed: a subagent line's own ``sessionId`` IS the parent
   session uuid), so filtering by the run's session_id captures BOTH the
   orchestrator's records AND its subagents' records while EXCLUDING unrelated
   concurrent sessions sharing the same config root. When the result object lacks a
   session_id, the harness falls back to a time-window scope around the run and logs
   that the scope is approximate;
4. **tags every record** with ``run=<i>`` (always — this makes the per-run CV
   live), a derived ``phase`` (so per-phase rows are non-empty), and the
   kaizen ``cycle`` (when supplied), resolving Cycle-1's documented run/phase/cycle
   residue.

SECURITY: the scenario prompt is target-adjacent DATA. It is passed as a single
``argv`` element — never interpolated into a shell string — and transcript /
result content is parsed with ``json.loads`` only (no ``eval`` / ``exec``).
Stdlib-only.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from scripts.tokenmeter_render import render_jsonl, to_daily_rollup
from scripts.tokenmeter_result import classify_result, parse_result
from scripts.tokenmeter_scenario import Scenario
from scripts.tokenmeter_schema import assemble
from scripts.tokenmeter_static import static_footprint
from scripts.tokenmeter_transcript import collect_usage_records

_LOG = logging.getLogger(__name__)

# A runner matching the FakeCliRunner / real_cli_runner shape: an awaitable call
# taking (argv, cwd) and resolving to the raw result (str | bytes | mapping).
Runner = Callable[[Sequence[str], Any], Awaitable[Any]]

#: Slack (ms) added on each side of the time-window fallback used ONLY when a run's
#: result envelope carries no ``session_id`` to scope by. Generous enough to admit a
#: transcript flushed slightly before/after the result returns; the fallback is
#: explicitly approximate (and logged as such), never the primary path.
_WINDOW_MARGIN_MS = 5_000

# ── Derived phase axis ───────────────────────────────────────────────────────

PHASE_ORCHESTRATE = "orchestrate"
PHASE_IMPLEMENT = "implement"

#: Substring → phase heuristic for deriving a record's phase from its sidechain
#: agent label (the kaizen pipeline phases: recon/design/implement/review/pr).
#: First match wins; an unmatched sidechain defaults to ``implement`` and the
#: orchestrator (non-sidechain) session to ``orchestrate``. This is a labelled
#: HEURISTIC, not ground truth — callers can pass a custom ``phase_resolver``.
_ROLE_PHASE_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("architect", "design"),
    ("design", "design"),
    ("research", "recon"),
    ("analy", "recon"),
    ("recon", "recon"),
    ("review", "review"),
    ("sdet", "review"),
    ("qa", "review"),
    ("test", "review"),
    ("security", "review"),
    ("writer", "pr"),
    ("doc", "pr"),
    ("release", "pr"),
    ("pm", PHASE_ORCHESTRATE),
    ("manager", PHASE_ORCHESTRATE),
    ("lead", PHASE_ORCHESTRATE),
)


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` off a record (object or mapping), with a default."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def default_phase_resolver(rec: Any) -> str:
    """Derive a non-null phase for a record (so per-phase rows are never empty).

    The orchestrator (non-sidechain) session is ``orchestrate``; a sidechain
    (sub-agent) maps its ``agent_label`` through :data:`_ROLE_PHASE_KEYWORDS`,
    defaulting to ``implement``. Always returns a string — never ``None`` — so the
    schema's per-phase grouping has at least one row on real output.
    """
    if not _get(rec, "is_sidechain", False):
        return PHASE_ORCHESTRATE
    label = (_get(rec, "agent_label") or "").lower()
    for needle, phase in _ROLE_PHASE_KEYWORDS:
        if needle in label:
            return phase
    return PHASE_IMPLEMENT


# ── Headless-claude argv / env posture (mirrors the proven sibling) ──────────

#: Default ``--permission-mode`` for the headless benchmark run. ``acceptEdits``
#: mirrors the proven sibling (atelier ``cli_dispatch.DEFAULT_PERMISSION_MODE``): in
#: headless ``-p`` it auto-accepts without a human prompt (so a measurement run
#: never HANGS waiting on a permission dialog) yet still routes through the
#: permission layer — NOT ``bypassPermissions`` (which disables the layer entirely).
#: Overridable per call so a strictly read-only scenario may pass ``plan``.
DEFAULT_PERMISSION_MODE = "acceptEdits"

#: The four documented Claude Code permission modes (CLI validation surface).
PERMISSION_MODES = ("default", "acceptEdits", "bypassPermissions", "plan")

#: Subprocess env allowlist for :func:`real_claude_runner` — mirrors atelier
#: ``cli_dispatch.ENV_ALLOWLIST``. ONLY these names (plus the ``LC_*`` locale
#: prefix) are forwarded to ``claude``; the full parent env (secrets, cloud creds,
#: ``ANTHROPIC_API_KEY``, ``GH_TOKEN``) is DROPPED so an autonomous benchmark run
#: cannot exfiltrate them. ``CLAUDE_CONFIG_DIR`` is load-bearing in BOTH directions:
#: ``claude`` reads its **subscription credentials** from it AND writes its
#: transcripts under it, so the AMBIENT value (or a seeded ``config_dir=`` override)
#: must be forwarded intact — relocating it to an empty dir is exactly the bug this
#: harness avoids. ``HOME`` is also forwarded (some auth/config lives under
#: ``$HOME/.claude``).
_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "TERM",
        "TZ",
        "TMPDIR",
        "XDG_RUNTIME_DIR",
        "XDG_DATA_DIRS",
        "XDG_CONFIG_HOME",
        "SHELL",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_CREDENTIALS_PATH",
    }
)

#: Names scrubbed even if a future edit widens the allowlist — secrets a benchmark
#: run must NEVER see. ``ANTHROPIC_API_KEY`` is doubly dangerous: forwarding it would
#: silently flip the run to API billing AND hand the key to an autonomous agent.
_ENV_DENYLIST_NEVER: frozenset[str] = frozenset({"ANTHROPIC_API_KEY", "GH_TOKEN", "GITHUB_TOKEN"})


def _build_subprocess_env(parent_env: dict[str, str] | None = None) -> dict[str, str]:
    """Return the minimal allowlisted env for the real ``claude`` subprocess.

    Mirrors atelier ``cli_dispatch.build_subprocess_env``: forwards ONLY
    :data:`_ENV_ALLOWLIST` names (plus the ``LC_*`` prefix) from ``parent_env``
    (default ``os.environ``); everything else — including ``ANTHROPIC_API_KEY`` /
    ``GH_TOKEN`` / arbitrary secrets — is dropped. ``CLAUDE_CONFIG_DIR`` survives
    because it is on the allowlist: by default it is the ambient value (carrying the
    user's subscription auth), or the seeded ``config_dir=`` override when one is
    set live inside the :func:`_claude_config_dir` window.
    """
    src = os.environ if parent_env is None else parent_env
    out: dict[str, str] = {}
    for name, value in src.items():
        if name in _ENV_DENYLIST_NEVER:
            continue
        if name in _ENV_ALLOWLIST or name.startswith("LC_"):
            out[name] = value
    return out


# ── The injectable real runner (the ONLY new external call) ──────────────────


async def real_claude_runner(argv: Sequence[str], cwd: Any = None) -> str:
    """Shell the real ``claude`` headless and return its stdout (DEFAULT runner).

    The ONLY new external call in the meter. Uses
    ``asyncio.create_subprocess_exec`` with an argv LIST (never ``shell=True``), so
    the scenario prompt — carried as a single argv element — is passed as data, not
    interpreted by a shell. The argv the harness builds forwards ``--model`` (so the
    report's control vector reflects the model actually run, not a decorative label)
    and ``--permission-mode`` (a safe headless posture).

    SECURITY: the child receives ONLY :func:`_build_subprocess_env`'s allowlist
    (PATH/HOME/locale + ``CLAUDE_CONFIG_DIR``), NOT the full parent env — so an
    autonomous benchmark run never sees ``ANTHROPIC_API_KEY`` / ``GH_TOKEN`` / cloud
    creds. ``CLAUDE_CONFIG_DIR`` is the AMBIENT (or seeded-override) value carrying
    subscription auth — it is NOT relocated to an empty per-run dir (that broke auth;
    see the module docstring). ``start_new_session=True`` puts the child in its own
    process group so a wall-clock-bounded caller can group-reap a hung ``claude``
    cleanly.

    Returns the captured stdout as text; an empty / failed run yields an empty-ish
    blob that :func:`parse_result` rejects, which :func:`classify_result` then maps
    to FAILURE (a failed run is never a $0 success). Tests never reach this path —
    they inject a fake runner.
    """
    proc = await asyncio.create_subprocess_exec(  # nosec B603 B607 - argv list, no shell; claude on PATH; env is a minimal allowlist
        *argv,
        cwd=str(cwd) if cwd is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_build_subprocess_env(),
        start_new_session=True,
    )
    stdout, _stderr = await proc.communicate()
    return stdout.decode("utf-8", errors="replace")


@contextlib.contextmanager
def _claude_config_dir(config_dir: Path):
    """Point ``$CLAUDE_CONFIG_DIR`` at ``config_dir`` for the duration of a run.

    Used ONLY on the optional ``config_dir=`` override path (a caller that has
    seeded credentials into a dedicated dir). The prior value is restored on exit so
    the harness never leaks env state across runs. The DEFAULT path does NOT enter
    this context — it runs ``claude`` against the ambient ``$CLAUDE_CONFIG_DIR`` so
    subscription auth works.
    """
    key = "CLAUDE_CONFIG_DIR"
    prev = os.environ.get(key)
    os.environ[key] = str(config_dir)
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = prev


@contextlib.contextmanager
def _maybe_config_dir(config_dir: Path | None):
    """Relocate ``$CLAUDE_CONFIG_DIR`` only when an override is supplied (else no-op).

    ``config_dir is None`` (the default) yields without touching the environment, so
    ``claude`` authenticates against the ambient config. A non-``None`` override
    enters :func:`_claude_config_dir` for the run.
    """
    if config_dir is None:
        yield
    else:
        with _claude_config_dir(config_dir):
            yield


# ── N-run harvest ────────────────────────────────────────────────────────────


def _now_ms() -> int:
    """Wall-clock epoch milliseconds (impure — used only for the time-window fallback)."""
    return int(time.time() * 1000)


def _scope_records(
    records: Sequence[Any],
    *,
    session_id: str | None,
    window: tuple[int, int],
    run_index: int,
) -> list[Any]:
    """Scope a config-root harvest to ONE run's records (auth-preserving isolation).

    Primary path: filter to records whose ``session_id`` equals the run's
    ``session_id``. Because Seam B reparents sidechain/subagent lines to the parent
    ``session_id``, this captures the orchestrator AND its subagents while excluding
    unrelated concurrent sessions sharing the config root.

    Fallback (``session_id`` is ``None`` — the result envelope didn't carry one):
    scope by the wall-clock ``window`` around the run and log that the scope is
    APPROXIMATE (it may admit a session that overlapped the window). No log is
    emitted when there were no records to scope at all.
    """
    if session_id:
        return [r for r in records if _get(r, "session_id") == session_id]
    start, end = window
    scoped = [
        r for r in records if (ts := _get(r, "ts_epoch_ms")) is not None and start <= ts <= end
    ]
    if records:
        _LOG.warning(
            "tokenmeter run %d: result envelope carried no session_id; scoping "
            "Seam-B transcripts by an APPROXIMATE time window [%d, %d] ms — it may "
            "admit a session that overlapped the run window",
            run_index,
            start,
            end,
        )
    return scoped


async def _harvest_async(
    scenario: Scenario,
    *,
    n: int,
    runner: Runner,
    cwd: Any,
    config_dir: Path | None,
    phase_resolver: Callable[[Any], str],
    cycle: str | None,
    model: str,
    permission_mode: str,
) -> tuple[list[Any], dict[str, float] | None, list[Any]]:
    """Run the scenario ``n`` times and return ``(tagged_records, oracle, statuses)``.

    Runs are SEQUENTIAL (agentic runs are non-deterministic). By default ``claude``
    runs against the ambient ``$CLAUDE_CONFIG_DIR`` (so subscription auth works) and
    each run's records are isolated by ``session_id`` (NOT by relocating the config
    dir); an optional ``config_dir`` override targets a dir the caller has seeded
    with credentials. The oracle is the SUM of the per-run ``total_cost_usd`` (the
    Seam-A cost oracle reconciled against the Seam-B computed total in
    :func:`~scripts.tokenmeter_schema.assemble`); it is ``None`` only when no run
    yielded a parseable result.

    The argv forwards ``--model`` (only when ``model`` is set) so the SAME model
    string that seeds ``metadata.model`` (and thus the control vector + pricing) is
    what ``claude`` actually ran — drift between before/after is therefore caught by
    the control-vector gate instead of slipping past a decorative label — and
    ``--permission-mode`` for a safe headless posture.
    """
    argv_base = ["claude", "-p", scenario.prompt, "--output-format", "json"]
    if model:
        argv_base += ["--model", model]
    argv_base += ["--permission-mode", permission_mode]
    records: list[Any] = []
    oracle_total = 0.0
    have_oracle = False
    statuses: list[Any] = []

    for i in range(1, n + 1):
        start_ms = _now_ms()
        with _maybe_config_dir(config_dir):
            raw = await runner(list(argv_base), cwd)
        end_ms = _now_ms()

        statuses.append(classify_result(raw))
        try:
            result = parse_result(raw)
        except (ValueError, TypeError):
            result = None
        session_id = result.session_id if result is not None else None
        if result is not None:
            oracle_total += result.total_cost_usd
            have_oracle = True

        all_records = collect_usage_records(config_dir=config_dir)
        scoped = _scope_records(
            all_records,
            session_id=session_id,
            window=(start_ms - _WINDOW_MARGIN_MS, end_ms + _WINDOW_MARGIN_MS),
            run_index=i,
        )

        run_label = str(i)
        for rec in scoped:
            phase = _get(rec, "phase") or phase_resolver(rec)
            records.append(dataclasses.replace(rec, run=run_label, phase=phase, cycle=cycle))

    oracle = {"total_cost_usd": oracle_total} if have_oracle else None
    return records, oracle, statuses


def _harvest(
    scenario: Scenario,
    *,
    n: int,
    runner: Runner | None,
    cwd: Any,
    config_dir: str | Path | None,
    phase_resolver: Callable[[Any], str] | None,
    cycle: str | None,
    model: str = "",
    permission_mode: str = DEFAULT_PERMISSION_MODE,
) -> tuple[list[Any], dict[str, float] | None, list[Any]]:
    """Sync wrapper: validate args and drive the async harvest.

    ``config_dir`` is the OPTIONAL override (``None`` → run against the ambient
    config, the auth-preserving default). The harness never creates or deletes the
    config dir — it harvests the (possibly shared) transcript root read-only and
    scopes by session_id, so a real ``~/.claude`` is never mutated.
    """
    if runner is None:
        raise ValueError(
            "a 'runner' is required (inject a FakeCliRunner in tests, "
            "or pass real_claude_runner for a live run)"
        )
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    resolver = phase_resolver or default_phase_resolver
    cfg = Path(config_dir) if config_dir is not None else None
    return asyncio.run(
        _harvest_async(
            scenario,
            n=n,
            runner=runner,
            cwd=cwd,
            config_dir=cfg,
            phase_resolver=resolver,
            cycle=cycle,
            model=model,
            permission_mode=permission_mode,
        )
    )


def _build_metadata(
    scenario: Scenario, *, n: int, metadata: dict[str, Any] | None
) -> dict[str, Any]:
    """Build the assemble metadata from the scenario + caller overrides.

    The scenario's ``target`` / ``scenario_source`` / ``cycles`` / ``subject`` seed
    the header; ``scenario_hash`` is FORCED from the scenario (it is the
    comparability control the before/after gate keys on, so a caller cannot
    accidentally break it). ``model`` / ``transport`` / ``effort`` /
    ``target_commit`` come from the caller's ``metadata`` (``model`` drives pricing
    AND the control vector). ``n_runs`` is the REQUESTED run count.
    """
    md = dict(metadata or {})
    md.setdefault("target", scenario.target)
    md.setdefault("scenario_source", scenario.source)
    md.setdefault("cycles", scenario.cycles)
    md.setdefault("subject", scenario.subject)
    md["scenario_hash"] = scenario.scenario_hash
    md["n_runs"] = n
    return md


def _write_evidence(
    records: Sequence[Any],
    *,
    evidence_out: str | Path | None,
    rollup_out: str | Path | None,
    default_model: str,
) -> None:
    """Persist the per-call Seam-B evidence reconstructed from the harvested records.

    The on-disk transcripts under the (possibly shared) ``$CLAUDE_CONFIG_DIR`` are
    not owned by the harness — they are NOT deleted (a real ``~/.claude`` must never
    be mutated). The PARSED, session-scoped records survive in memory, so the §5
    per-call JSONL evidence (``evidence_out``) and the §7 tokscale-compatible
    daily-rollup (``rollup_out``) are reconstructed from them here. No-op when
    neither path is supplied — the assembled report stays the sole output.
    """
    if evidence_out:
        text = render_jsonl(records, default_model=default_model)
        Path(evidence_out).write_text((text + "\n") if text else "", encoding="utf-8")
    if rollup_out:
        rollup = to_daily_rollup(records, default_model=default_model)
        Path(rollup_out).write_text(json.dumps(rollup, indent=2) + "\n", encoding="utf-8")


# ── Public entry points ──────────────────────────────────────────────────────


def run_scenario(
    scenario: Scenario,
    *,
    n: int = 3,
    runner: Runner | None = None,
    cwd: Any = None,
    config_dir: str | Path | None = None,
    phase_resolver: Callable[[Any], str] | None = None,
    cycle: str | None = None,
    outcomes: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    evidence_out: str | Path | None = None,
    rollup_out: str | Path | None = None,
) -> dict[str, Any]:
    """Replay ``scenario`` ``n`` times and assemble the DYNAMIC-only report.

    The N runs are folded into a single ``BenchmarkReport`` via the Cycle-1
    :func:`~scripts.tokenmeter_schema.assemble`, so every dynamic figure carries
    ``{n, mean, cv, confidence}`` (``n=3`` by default; ``cv`` is non-null once >1
    run produces records). No static footprint is included — use
    :func:`benchmark_target` for static + dynamic.

    By default ``claude`` runs against the ambient ``$CLAUDE_CONFIG_DIR`` (so
    subscription auth works) and each run's records are isolated by ``session_id``;
    pass ``config_dir`` ONLY when you have seeded credentials into a dedicated dir.
    The ``model`` from ``metadata`` is forwarded to ``claude`` via ``--model`` (so
    the report's control vector is not decorative); the per-run statuses are folded
    into the report's ``runs`` block (so an all-FAILURE harvest cannot read clean);
    and ``evidence_out`` / ``rollup_out``, when set, persist the per-call JSONL +
    daily-rollup reconstructed from the session-scoped records.
    """
    model = (metadata or {}).get("model") or ""
    records, oracle, statuses = _harvest(
        scenario,
        n=n,
        runner=runner,
        cwd=cwd,
        config_dir=config_dir,
        phase_resolver=phase_resolver,
        cycle=cycle,
        model=model,
        permission_mode=permission_mode,
    )
    md = _build_metadata(scenario, n=n, metadata=metadata)
    _write_evidence(records, evidence_out=evidence_out, rollup_out=rollup_out, default_model=model)
    return assemble(
        records, [], outcomes=outcomes or {}, oracle=oracle, metadata=md, run_statuses=statuses
    )


def benchmark_target(
    scenario: Scenario,
    *,
    n: int = 3,
    runner: Runner | None = None,
    cwd: Any = None,
    config_dir: str | Path | None = None,
    phase_resolver: Callable[[Any], str] | None = None,
    cycle: str | None = None,
    outcomes: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    repo_root: str | Path | None = None,
    tokenizer: Any = None,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    evidence_out: str | Path | None = None,
    rollup_out: str | Path | None = None,
) -> dict[str, Any]:
    """ONE report combining the static footprint + the dynamic N-run aggregate.

    The static footprint (:func:`~scripts.tokenmeter_static.static_footprint`) is
    measured on ``scenario.target`` (resolved against ``repo_root`` or CWD) and
    rendered as ``overhead`` rows; the dynamic N-run records become the category /
    phase / role rows. This is what the CLI ``benchmark`` subcommand emits and what
    a kaizen run writes to ``before.json`` / ``after.json`` for the delta.

    By default ``claude`` runs against the ambient ``$CLAUDE_CONFIG_DIR`` (auth
    works) and each run's records are isolated by ``session_id``; ``config_dir`` is
    the optional seeded-credentials override. The ``model`` from ``metadata`` is
    forwarded to ``claude`` via ``--model`` (so the control vector reflects the model
    actually run); the per-run statuses ride the report's ``runs`` block (fail-loud);
    and ``evidence_out`` / ``rollup_out``, when set, persist the per-call JSONL +
    daily-rollup reconstructed from the session-scoped records.
    """
    model = (metadata or {}).get("model") or ""
    records, oracle, statuses = _harvest(
        scenario,
        n=n,
        runner=runner,
        cwd=cwd,
        config_dir=config_dir,
        phase_resolver=phase_resolver,
        cycle=cycle,
        model=model,
        permission_mode=permission_mode,
    )
    footprint = static_footprint(scenario.resolve_target(repo_root), tokenizer=tokenizer)
    md = _build_metadata(scenario, n=n, metadata=metadata)
    _write_evidence(records, evidence_out=evidence_out, rollup_out=rollup_out, default_model=model)
    return assemble(
        records,
        footprint,
        outcomes=outcomes or {},
        oracle=oracle,
        metadata=md,
        run_statuses=statuses,
    )
