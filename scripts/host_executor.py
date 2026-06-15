"""Host transport (M8a-2a) — Phase-4 implementation waves via atelier's engine.

This is the ``KAIZEN_TRANSPORT=host`` execution path for Phase 4 (the
implementation waves) of a kaizen cycle. Where the default ``bridge`` transport
dispatches Phase-4 implementers through CC Agent-Teams + the SQLite queue, the
host transport translates the SAME validated Action-Items DAG into atelier
v1.10.0's deterministic-host engine task dicts and drives
``host_scheduler.run_host_pipeline_for_project`` in-process (no subprocess hop)
via :func:`scripts.atelier_engine.atelier_engine`.

SCOPE (M8a-2a — PR 1 of 3):
  * ONLY Phase 4 (implement tasks) goes through the engine here. Phases 1-3
    (agenda / pre-analysis / synthesis meeting) stay orchestrator-side and are
    OUT OF SCOPE — :func:`host_cycle_executor` RECEIVES the already-validated
    Action-Items list as input.
  * NO review tasks (Phase 5b' review-pairing is M8a-2b).
  * NO CI mirror, NO ``commit_cycle`` here (M8a-2c / orchestrator). This module
    returns the interpreted outcome dict and leaves the merged files in the
    clone; the caller commits + mirrors CI.

CLOSURE / RE-IMPORT HAZARD (see :mod:`scripts.atelier_engine`): inside the
``with atelier_engine(...)`` window the name ``scripts`` resolves to ATELIER's
package, so kaizen-only ``scripts.*`` modules are NOT importable. Every kaizen
reference the engine calls back into (the rendered briefing text, the model
policy, the logger) is therefore PRE-BOUND before the window — the closures in
this module capture concrete kaizen objects/strings at construction time and
perform NO ``scripts.*`` import at call time.

The ENTIRE ``asyncio.run(run_host_pipeline_for_project(...))`` runs INSIDE the
window: the coroutine touches atelier ``scripts.*`` (cli_dispatch, dag,
run_mode, …) throughout its lifetime, so closing the window before the coroutine
completes would raise mid-flight ImportError.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

# PRE-BOUND kaizen references — captured at module import (kaizen `scripts.*`),
# so any closure that uses them inside the engine window references the kaizen
# object, never an in-window atelier re-import. See the module docstring's
# closure/re-import hazard note.
from scripts.atelier_engine import assert_engine_available, atelier_engine

_log = logging.getLogger("kaizen.host_executor")

# The constant `phase` value every Phase-4 implement task carries (matches the
# engine's free-form `phase` field; used by the model policy + briefing).
_IMPLEMENTATION_PHASE = "implementation"

# Fallback persona when an Action Item carries no `owner` — mirrors
# team_executor's `pm = roster[0] if roster else "pm-1"` convention.
_DEFAULT_PM_ROLE = "pm-1"


# ── Deliverable 1a — DAG → engine task translation ──────────────────────────


def build_engine_tasks(
    action_items: Sequence[Mapping[str, Any]],
    waves: Sequence[Sequence[str]],
    *,
    pm: str = _DEFAULT_PM_ROLE,
) -> list[dict[str, Any]]:
    """Translate validated kaizen Action Items into engine Phase-4 task dicts.

    Field mapping (kaizen Action Item → engine task):

    =====================  ======================================================
    engine field           source
    =====================  ======================================================
    ``task_id``            item ``id``
    ``parallel_group``     0-based wave index from ``waves`` (all ids in
                           ``waves[k]`` get ``parallel_group=k``)
    ``depends_on``         item ``depends_on`` (verbatim, list)
    ``writes``             item ``touches`` (repo-relative — the disjointness key
                           the engine uses for write-isolation)
    ``reads``              item ``reads`` (verbatim, list)
    ``assigned_persona``   item ``owner`` (falls back to ``pm`` when absent/empty)
    ``phase``              constant ``"implementation"``
    =====================  ======================================================

    ``waves`` is :attr:`scripts.dag.ValidationResult.waves` (tuple-of-tuples of
    ids; ``waves[0]`` is Wave 1). Phase-4 IMPLEMENT tasks only — no review tasks.

    Raises ``KeyError`` if an item id is not present in any wave (a caller bug —
    ``waves`` must be the validation result for THIS ``action_items`` list).
    """
    # id -> 0-based wave index (parallel_group).
    group_of: dict[str, int] = {}
    for wave_idx, wave in enumerate(waves):
        for item_id in wave:
            group_of[item_id] = wave_idx

    tasks: list[dict[str, Any]] = []
    for item in action_items:
        item_id = item["id"]
        if item_id not in group_of:
            raise KeyError(
                f"build_engine_tasks: item {item_id!r} is not present in any wave; "
                f"`waves` must be the validate_dag(...) result for THIS item list"
            )
        owner = item.get("owner")
        persona = owner if owner else pm
        tasks.append(
            {
                "task_id": item_id,
                "parallel_group": group_of[item_id],
                "depends_on": list(item.get("depends_on") or []),
                "writes": list(item.get("touches") or []),
                "reads": list(item.get("reads") or []),
                "assigned_persona": persona,
                "phase": _IMPLEMENTATION_PHASE,
            }
        )
    return tasks


# ── Deliverable 2 — pre-bound closures (F7-trailer stripped) ────────────────


# Stable anchor phrases for the THREE team-mode comms paragraphs the rendered
# Phase-4 briefing carries, in document order:
#   1. the per-template "Reply format" paragraph (OK:/BLOCKED: + SendMessage),
#   2. the terse-output rule (references the SendMessage / shutdown_response JSON),
#   3. the F7 trailer (TEAMMATE_REPLY_RULE — SendMessage(to="team-lead") + shutdown).
# ALL THREE are team-mode-only and reference comms primitives that do not exist in
# host mode (no team-lead, no SendMessage, no shutdown handshake), so host mode
# cuts at the EARLIEST of them. The reply-format anchor is the byte-frozen opening
# of `phase_4_implementation.md`'s reply-format line.
_REPLY_FORMAT_ANCHOR = "IMPORTANT — Reply format:"


def _strip_f7_trailer(rendered: str, trailer: str) -> str:
    """Strip ALL team-mode comms paragraphs from a rendered Phase-4 briefing.

    The team-mode rendered body carries three trailing team-only paragraphs —
    the "Reply format" OK:/BLOCKED: rule, the terse-output rule (both reference
    ``SendMessage``), and the F7 trailer (``SendMessage(to="team-lead")`` +
    shutdown JSON). ALL are MEANINGLESS in host mode: the engine worker emits a
    terminal ``task_result`` envelope, not a SendMessage; there is no team-lead
    and no shutdown handshake. Leaving ANY of them would instruct a host worker
    to use a primitive it does not have.

    We cut at the EARLIEST comms anchor found — the "Reply format" paragraph
    opener, else the F7 trailer span. The caller re-appends a host-specific
    terminal-envelope instruction. Returns the surviving body, right-stripped.
    """
    candidates: list[int] = []
    rf_idx = rendered.find(_REPLY_FORMAT_ANCHOR)
    if rf_idx != -1:
        candidates.append(rf_idx)
    t_idx = rendered.rfind(trailer)
    if t_idx != -1:
        candidates.append(t_idx)
    if not candidates:
        # No comms paragraph present — return as-is (nothing to strip).
        # Defensive: the team template always carries them, but a future
        # trailer-less template must not crash the host path.
        return rendered.rstrip()
    return rendered[: min(candidates)].rstrip()


def _make_briefing_for(
    items_by_id: Mapping[str, Mapping[str, Any]],
    group_of: Mapping[str, int],
    phase_4_implementer: Callable[..., str],
    teammate_reply_rule: str,
) -> Callable[[Mapping[str, Any], int], str]:
    """Build a ``briefing_for(task, attempt) -> str`` closure for host workers.

    All kaizen references are PRE-BOUND arguments captured before the engine
    window: ``phase_4_implementer`` (the kaizen template fn), ``teammate_reply_rule``
    (the F7 trailer text to locate + strip), and the per-item lookup maps. The
    closure performs NO ``scripts.*`` import at call time, so it renders correctly
    even while the ``scripts`` name resolves to atelier inside the window.

    The closure renders kaizen's Phase-4 implementer template for the item, then
    STRIPS the F7 SendMessage/shutdown trailer (host workers emit a terminal
    ``task_result`` envelope, not a SendMessage) and appends a host-specific
    terminal-envelope instruction.
    """
    # The byte-frozen trailer span used to locate + cut the F7 paragraph.
    trailer = teammate_reply_rule.strip()

    # Host-mode terminal instruction — replaces the F7 SendMessage/shutdown
    # contract. Mirrors the atelier e2e reference _briefing_for pattern: the
    # worker emits ONLY the terminal task_result envelope.
    host_terminal_rule = (
        "When the change is applied to disk in the CURRENT working directory "
        "(already your task's isolated worktree — use bare relative paths, do "
        "not change directories), emit ONLY the terminal task_result envelope "
        "matching the provided json-schema: status 'done' on success (with one "
        "artifact per file you wrote), or status 'blocked' with a one-line "
        "notes_md naming the obstacle if you cannot complete the change. The "
        "envelope is your sole output channel — do not narrate. Do nothing else."
    )

    def briefing_for(task: Mapping[str, Any], attempt: int) -> str:
        item_id = str(task["task_id"])
        item = items_by_id[item_id]
        wave_n = group_of[item_id] + 1  # template is 1-based
        rendered = phase_4_implementer(item=dict(item), wave_n=wave_n)
        body = _strip_f7_trailer(rendered, trailer)
        return f"{body}\n\n{host_terminal_rule}"

    return briefing_for


# Kaizen model policy per CLAUDE.md: implementers run opus on high effort.
# Pure mapping over persona/phase — no `scripts.*` import at call time.
_IMPLEMENTER_MODEL = "opus"


def _make_model_for() -> Callable[[Mapping[str, Any], int], str]:
    """Build a ``model_for(task, attempt) -> str`` closure.

    Pure: kaizen's model policy over ``task["assigned_persona"]`` /
    ``task["phase"]``. CLAUDE.md recommends opus (high effort) for Phase-4
    implementers; the engine resolves the ``opus`` alias to the current Opus
    model. No ``scripts.*`` import at call time.
    """

    def model_for(task: Mapping[str, Any], attempt: int) -> str:
        # Phase-4 implementers → opus (the only phase this PR dispatches; any
        # future phase keeps the implementer floor until a per-phase policy lands).
        return _IMPLEMENTER_MODEL

    return model_for


def _make_escalate_fn(
    log: logging.Logger,
) -> Callable[[Mapping[str, Any]], None]:
    """Build an ``escalate_fn(record) -> None`` closure with the module logger
    PRE-BOUND (captured before the engine window). No ``scripts.*`` import at
    call time."""

    def escalate_fn(record: Mapping[str, Any]) -> None:
        log.warning("host_executor escalation: %r", dict(record))

    return escalate_fn


# ── Deliverable 1b — host cycle executor ────────────────────────────────────


def _interpret_engine_results(
    results: Sequence[Mapping[str, Any]] | Sequence[Any],
    tasks: Sequence[Mapping[str, Any]],
    *,
    participants: Sequence[str],
    subject: str | None,
    is_failed_attempt: Callable[[Any], bool],
) -> dict[str, Any]:
    """Interpret the engine's per-task results into a kaizen outcome dict.

    Success iff EVERY task returned a terminal, non-failed envelope with
    ``status == "done"``. Any failed-attempt sentinel or any non-``done`` status
    → ``no_consensus`` abandonment (mirroring team mode's treatment of an
    implementation phase that could not complete).

    Returns the SAME outcome-dict shape as ``team_cycle_executor`` (success or
    abandoned variant; see ``scripts/team_executor.py`` module docstring).
    ``is_failed_attempt`` is the atelier predicate, passed in (resolved in-window
    by the caller) so this pure interpreter needs no atelier import.
    """
    failed: list[str] = []
    for task, result in zip(tasks, results, strict=True):
        tid = task["task_id"]
        if is_failed_attempt(result):
            failed.append(f"{tid}: failed-attempt sentinel")
            continue
        status = result.get("status") if isinstance(result, Mapping) else None
        if status != "done":
            failed.append(f"{tid}: status={status!r}")

    if failed:
        return {
            "status": "abandoned",
            "subject": subject,
            "participants": list(participants),
            "phase_reached": "implementation",
            "reason": "no_consensus",
            "detail": (
                "host Phase-4 engine pipeline did not reach all-`done`: " + "; ".join(failed)
            ),
            "artifacts": [],
        }

    return {
        "status": "success",
        "subject": subject,
        "commit_sha": None,  # commit is the orchestrator's job (M8a-2c)
        "minutes_memex_slug": None,
        "participants": list(participants),
    }


def host_cycle_executor(
    *,
    action_items: Sequence[Mapping[str, Any]],
    existing_files: frozenset[str] | set[str] | Sequence[str],
    clone_dir: str | Path,
    roster: Sequence[str] | None = None,
    pm: str | None = None,
    subject: str | None = None,
    budget_total_tokens: int = 4_000_000,
    runner: Any = None,
    journal_path: str | Path | None = None,
) -> dict[str, Any]:
    """Run a cycle's Phase-4 implementation waves through atelier's host engine.

    RECEIVES the already-validated Action-Items DAG (Phases 1-3 stay
    orchestrator-side — OUT OF SCOPE here). Returns the SAME outcome-dict shape
    as :func:`scripts.team_executor.team_cycle_executor` (success | abandoned).

    Steps:
      1. :func:`scripts.dag.validate_dag` — re-validate (REUSE; do not reinvent).
         Invalid → ``no_consensus`` abandonment (mirrors team mode).
      2. :func:`build_engine_tasks` — translate to engine Phase-4 task dicts.
      3. Build closures with PRE-BOUND refs BEFORE the engine window.
      4. :func:`assert_engine_available` — OUTSIDE the window (version+capability
         gate).
      5. ``with atelier_engine(root) as host:`` — construct BudgetPool /
         ResultJournal / sandbox / worktree-factory / neutral run_mode IN-window
         (these are atelier's), then ``asyncio.run(run_host_pipeline_for_project(...))``
         ENTIRELY inside the window.
      6. OUTSIDE the window: interpret results → outcome dict.

    ``runner`` defaults to ``None`` → atelier's ``real_cli_runner`` resolved
    in-window. Tests inject a ``FakeCliRunner`` (no real ``claude``).
    """
    clone_path = Path(clone_dir)
    roster = list(roster or [])
    pm = pm or (roster[0] if roster else _DEFAULT_PM_ROLE)
    participants = roster if roster else [pm]
    existing = frozenset(existing_files)

    # Reuse kaizen's DAG re-validator (pre-bound import at module top).
    from scripts.dag import validate_dag

    validation = validate_dag(list(action_items), existing_files=existing)
    if not validation.ok:
        return {
            "status": "abandoned",
            "subject": subject,
            "participants": list(participants),
            "phase_reached": "implementation",
            "reason": "no_consensus",
            "detail": (
                "host Phase-4 DAG validation failed: "
                + "; ".join(str(e) for e in validation.errors)
            ),
            "artifacts": [],
        }

    tasks = build_engine_tasks(action_items, validation.waves, pm=pm)

    # ── Pre-bind ALL kaizen refs the engine will call back into, BEFORE the
    #    window. Inside the window `scripts` == atelier, so these imports MUST
    #    happen now. ────────────────────────────────────────────────────────
    from scripts.dispatch_templates import TEAMMATE_REPLY_RULE, phase_4_implementer

    # Per-item lookup maps the briefing closure reads (item body + 1-based wave).
    # These are plain kaizen dicts captured here, OUTSIDE the window — the closure
    # references them by value, never re-deriving them via a `scripts.*` import.
    items_by_id = {item["id"]: item for item in action_items}
    group_of: dict[str, int] = {}
    for wi, wave in enumerate(validation.waves):
        for iid in wave:
            group_of[iid] = wi

    briefing_for = _make_briefing_for(
        items_by_id, group_of, phase_4_implementer, TEAMMATE_REPLY_RULE
    )
    model_for = _make_model_for()
    escalate_fn = _make_escalate_fn(_log)

    # Version + capability gate (OUTSIDE the window).
    atelier_root = assert_engine_available()

    journal_file = (
        Path(journal_path) if journal_path is not None else clone_path / ".ai" / "host-journal.json"
    )
    journal_file.parent.mkdir(parents=True, exist_ok=True)

    # ── The engine window. EVERYTHING atelier — BudgetPool, ResultJournal,
    #    sandbox, worktree factory, neutral run_mode, the runner default, AND
    #    the whole asyncio.run — happens here. ─────────────────────────────────
    with atelier_engine(atelier_root) as host:
        cli_dispatch = importlib.import_module("scripts.cli_dispatch")
        budget_pool_mod = importlib.import_module("scripts.budget_pool")
        result_journal_mod = importlib.import_module("scripts.result_journal")
        run_mode_mod = importlib.import_module("scripts.run_mode")

        budget = budget_pool_mod.BudgetPool(total_tokens=budget_total_tokens)
        journal = result_journal_mod.ResultJournal(journal_file)
        sandbox = cli_dispatch.native_sandbox_wrap(str(clone_path))
        wt = host.simple_worktree_factory(clone_path)
        # EXPLICIT neutral run mode so atelier's saved cost-lean profile does
        # NOT silently resize kaizen's budget/fleet (balanced == is_neutral).
        neutral_run_mode = run_mode_mod.resolve_run_mode(explicit="balanced")

        # runner=None → atelier's real_cli_runner resolved in-window. Tests
        # pass a FakeCliRunner instance.
        eff_runner = runner if runner is not None else cli_dispatch.real_cli_runner

        # disallowed-tools deny floor (Bash/WebFetch/WebSearch) — matches the
        # engine + e2e reference.
        disallowed = list(cli_dispatch.DEFAULT_DISALLOWED_TOOLS)

        # CRITICAL: the ENTIRE asyncio.run is INSIDE the window — the coroutine
        # touches atelier scripts.* throughout; closing early = ImportError.
        results = asyncio.run(
            host.run_host_pipeline_for_project(
                tasks,
                clone_dir=str(clone_path),
                budget=budget,
                journal=journal,
                existing_files=sorted(existing),
                model_for=model_for,
                briefing_for=briefing_for,
                worktree_factory=wt,
                runner=eff_runner,
                sandbox_wrap=sandbox,
                escalate_fn=escalate_fn,
                run_mode=neutral_run_mode,
                disallowed_tools=disallowed,
            )
        )
        is_failed_attempt = cli_dispatch.is_failed_attempt

    # ── Back OUTSIDE the window: interpret. (is_failed_attempt was captured as
    #    a bound callable inside the window; it remains callable here.) ────────
    return _interpret_engine_results(
        results,
        tasks,
        participants=participants,
        subject=subject,
        is_failed_attempt=is_failed_attempt,
    )
