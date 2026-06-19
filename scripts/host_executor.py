"""Host transport (M8a-2a) — Phase-4 implementation waves via atelier's engine.

This is the ``KAIZEN_TRANSPORT=host`` execution path for Phase 4 (the
implementation waves) of a kaizen cycle. Where the default ``bridge`` transport
dispatches Phase-4 implementers through CC Agent-Teams + the SQLite queue, the
host transport translates the SAME validated Action-Items DAG into atelier
v1.10.0's deterministic-host engine task dicts and drives
``host_scheduler.run_host_pipeline_for_project`` in-process (no subprocess hop)
via :func:`scripts.atelier_engine.atelier_engine`.

SCOPE (M8a-2a + M8a-2b + M8a-2c):
  * ONLY Phase 4 (implement tasks) goes through the engine here. Phases 1-3
    (agenda / pre-analysis / synthesis meeting) stay orchestrator-side and are
    OUT OF SCOPE — :func:`host_cycle_executor` RECEIVES the already-validated
    Action-Items list as input.
  * Phase 5b' review-pairing + fix loop runs in the SAME engine window on a clean
    Phase-4 success (M8a-2b).
  * M8a-2c made the host path SELF-CONTAINED: after the window closes,
    :func:`host_cycle_executor` runs the CI-mirror gate (baseline-diff parity
    with team mode), then COMMITS the merged work via
    :func:`scripts.cycle_git.commit_cycle_and_sha` and stamps the real
    ``commit_sha`` + Memex minutes slug. The commit lives INSIDE this executor
    (before run.py inspects the outcome) so F3 holds with ZERO run.py change.
    The default journal path lives OUTSIDE the clone so the commit's
    transient-dir strip cannot delete it mid-flight (§1A).

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
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

# PRE-BOUND kaizen references — captured at module import (kaizen `scripts.*`),
# so any closure that uses them inside the engine window references the kaizen
# object, never an in-window atelier re-import. See the module docstring's
# closure/re-import hazard note.
#
# C3 (M8a-2b): ALL kaizen helpers the review-fix loop reuses are imported HERE,
# at module top, OUTSIDE any `atelier_engine()` window. NEVER import
# `scripts.host_scheduler` / `scripts.planner` / `scripts.cli_dispatch` (atelier-
# only) here — those are reached ONLY inside the window via `importlib`. Every
# symbol below is kaizen's, captured before the swap so the briefing closures +
# consolidation + parse run correctly while `scripts` resolves to atelier.
from scripts.atelier_engine import assert_engine_available, atelier_engine
from scripts.ci_runner import parse_pytest_pass_count, run_ci_checks
from scripts.cycle_git import commit_cycle_and_sha
from scripts.fix_loop import (
    _BLOCKING_SEVERITIES,
    Finding,
    FixLoopState,
    build_abandonment_outcome,
    record_findings,
    should_continue,
    start_iteration,
)
from scripts.reviewers import InsufficientRosterError, select_reviewers

# REUSE VERBATIM (no edits — per the M8a-2b spec): the byte-sensitive reviewer
# finding-line parser and the finding->implementer owner router. Both are pure
# kaizen helpers in team_executor; importing them (rather than re-implementing)
# keeps host and team modes parsing the IDENTICAL `[severity] file:line — text`
# grammar and routing fixes to the SAME owner index. team_executor does not
# import host_executor, so there is no import cycle.
#
# M8a-2c: the CI-mirror gate + abandonment-reason mapping reuse team mode's
# byte-identical baseline-diff + reason helpers. `run_ci_checks` /
# `parse_pytest_pass_count` (ci_runner) and `_diff_ci_results` /
# `_pick_highest_reason` (team_executor) are imported HERE, at module top,
# OUTSIDE any `atelier_engine()` window — the CI gate + commit run at the
# OUTSIDE-window return seam by construction, so these are never reached while
# `scripts` resolves to atelier. (`_pick_highest_reason` encapsulates
# team_executor's `_CHECK_TO_REASON` map, so host and team modes emit the
# IDENTICAL per-check → abandonment-reason taxonomy — the §7 #7 parity test
# anchors that against `_CHECK_TO_REASON` directly.)
from scripts.team_executor import (
    _diff_ci_results,
    _find_owner_for_finding,
    _parse_reviewer_response,
    _pick_highest_reason,
)

_log = logging.getLogger("kaizen.host_executor")

# Reviewer-selection lens preferences — VERBATIM from team_executor.py:2258 so
# host and team modes pick the same disjoint reviewers given the same roster.
_REVIEWER_LENSES = ("security", "architect", "prompt", "safety")

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


# Stable anchor phrases for the THREE team-mode comms paragraphs a rendered
# kaizen briefing can carry, in document order:
#   1. the per-template "Reply format" paragraph (OK:/BLOCKED: + SendMessage) —
#      present on `phase_4_implementation.md`, ABSENT on the Phase-5 review/mesh
#      templates;
#   2. the always-on terse-output rule (`_inject_terse_before_trailer`), which
#      references the SendMessage / shutdown_response JSON protocol body — present
#      on EVERY teammate-bound template that routes through the terse injection;
#   3. the F7 trailer (TEAMMATE_REPLY_RULE — SendMessage(to="team-lead") + shutdown).
# ALL THREE are team-mode-only and reference comms primitives that do not exist in
# host mode (no team-lead, no SendMessage, no shutdown handshake), so host mode
# cuts at the EARLIEST of them. The anchors are the byte-frozen openings of those
# paragraphs.
#
# M8a-2b: the terse-rule anchor was ADDED to the candidate set. The Phase-4
# implementer template carries the "Reply format" paragraph BEFORE the terse rule,
# so for Phase-4 the cut point is unchanged (reply-format is still earliest). But
# the Phase-5 review / mesh templates have NO "Reply format" paragraph — their
# earliest comms anchor IS the terse rule, which references "SendMessage /
# shutdown_response JSON protocol body". Without the terse anchor a review brief
# would cut only the F7 trailer and SHIP the terse rule's SendMessage reference to
# a host worker that has no SendMessage. Generalizing the cut to the earliest of
# {reply-format, terse-rule, F7 trailer} fixes both phases with one rule.
_REPLY_FORMAT_ANCHOR = "IMPORTANT — Reply format:"
# Byte-frozen opening of `dispatch_templates._TERSE_OUTPUT_RULE` (B1 / caveman).
_TERSE_OUTPUT_ANCHOR = "IMPORTANT — Output shape (terse):"


def _strip_f7_trailer(rendered: str, trailer: str) -> str:
    """Strip ALL team-mode comms paragraphs from a rendered teammate briefing.

    The team-mode rendered body carries up to three trailing team-only paragraphs
    — the "Reply format" OK:/BLOCKED: rule (Phase-4 only), the always-on
    terse-output rule (references the SendMessage / shutdown_response JSON), and
    the F7 trailer (``SendMessage(to="team-lead")`` + shutdown JSON). ALL are
    MEANINGLESS in host mode: the engine worker emits a terminal ``task_result``
    envelope, not a SendMessage; there is no team-lead and no shutdown handshake.
    Leaving ANY of them would instruct a host worker to use a primitive it does
    not have.

    We cut at the EARLIEST comms anchor found — the "Reply format" paragraph
    opener, the terse-output-rule opener, or the F7 trailer span — whichever
    appears first. The caller re-appends a host-specific terminal-envelope
    instruction. Returns the surviving body, right-stripped.
    """
    candidates: list[int] = []
    rf_idx = rendered.find(_REPLY_FORMAT_ANCHOR)
    if rf_idx != -1:
        candidates.append(rf_idx)
    terse_idx = rendered.find(_TERSE_OUTPUT_ANCHOR)
    if terse_idx != -1:
        candidates.append(terse_idx)
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


# Kaizen per-phase model policy per CLAUDE.md model-rec: implementers,
# independent reviewers, AND fix-round implementers ALL run opus (high effort) —
# the reviewer must catch what the implementer missed (a shallow reviewer model
# defeats the whole F9 review loop), and the fix round re-runs implementer-grade
# work. All `opus` today; the per-PHASE seam is the point (M8a-2b) — a future tier
# tweak edits ONE mapping rather than threading a constant. Pure mapping over
# ``task["phase"]``; no `scripts.*` import at call time.
#
# `review` covers BOTH the round-1/round-2 reviewer tasks AND the PM-acceptance
# task (the PM gate is dispatched as a read-only `phase="review"` task — see
# `build_pm_task`), so the PM round resolves to opus through the same key.
_PHASE_MODEL_POLICY: dict[str, str] = {
    "implementation": "opus",
    "review": "opus",
    "fix": "opus",
}


def _make_model_for() -> Callable[[Mapping[str, Any], int], str]:
    """Build a ``model_for(task, attempt) -> str`` closure (per-phase policy).

    Pure: kaizen's per-phase model policy over ``task["phase"]`` via
    :data:`_PHASE_MODEL_POLICY`. CLAUDE.md recommends opus (high effort) for
    Phase-4 implementers, independent reviewers, and fix-round implementers
    alike; the engine resolves the ``opus`` alias to the current Opus model.

    FAIL LOUD on an unknown phase — a task carrying a ``phase`` the policy does
    not cover is a wiring bug (a new task kind that forgot its model rec), and
    silently defaulting it to some model would mask that bug and could under-power
    a safety-critical phase. We raise ``ValueError`` naming the phase so the
    omission surfaces at dispatch time, not in a degraded run. No ``scripts.*``
    import at call time (pre-bind safe).
    """

    def model_for(task: Mapping[str, Any], attempt: int) -> str:
        phase = task["phase"]
        try:
            return _PHASE_MODEL_POLICY[phase]
        except KeyError:
            raise ValueError(
                f"no model policy for phase {phase!r}; known phases: {sorted(_PHASE_MODEL_POLICY)}"
            ) from None

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


# ── M8a-2b — Phase 5b' review-pairing (re-homed Star→Mesh→Star) ─────────────
#
# The engine workers are ISOLATED — a host reviewer cannot SendMessage a peer.
# So the ORCHESTRATOR is the mesh fabric: round 1 (Star-1) collects each
# reviewer's independent findings; round 2 (Mesh) shows each reviewer the OTHER
# reviewers' round-1 findings and asks for a CONFIRM / RETRACT / ESCALATE verdict
# plus any net-new finding; the orchestrator then consolidates the verdicts
# (Star-2, pure) per the C4 severity-gated weeding rule.
#
# Every review/mesh/PM task is dispatched WITHOUT a `reviews` field, so the
# engine's own `build_review_pairing` derives an EMPTY pairing and NEVER enters
# its own (BLIND re-dispatch) review-fix loop — kaizen owns the loop and reuses
# `fix_loop.py` verbatim. See the spec's "VERIFIED ENGINE FINDING".

# The constant `phase` for read-only review / mesh / PM-acceptance tasks (resolves
# to `opus` via `_PHASE_MODEL_POLICY["review"]`).
_REVIEW_PHASE = "review"
# The constant `phase` for fix-round writer tasks (resolves to `opus`).
_FIX_PHASE = "fix"

# The host-mode terminal-envelope instruction appended to a READ-ONLY review/mesh/
# PM brief (replaces the F7 SendMessage/shutdown trailer). A read-only worker
# writes NOTHING — it inspects the merged change set via `git diff` in its cwd
# (the SHARED base clone — C1) and emits its verdict prose as `notes_md` on a
# terminal `done` envelope with EMPTY `artifacts` (legal for a read-only task —
# the engine's false-`done` guard is exempt for non-writers, host_scheduler.py:
# 1212-1217). The "run `git diff` in cwd" line closes the C1 "reviewer not given
# the diff" gotcha: the shared clone has every Phase-4 implementer's work merged
# into HEAD before any reviewer is dispatched (eager merge, host_scheduler.py:
# 1261-1277), so a `git diff HEAD~..HEAD` / `git show` over the cycle's merge
# commits IS the diff under review.
_REVIEW_TERMINAL_RULE = (
    "You are in the SHARED base clone (your current working directory); every "
    "Phase-4 implementer's change is ALREADY merged into HEAD. Run `git diff` "
    "(and `git log`/`git show`) in your cwd to inspect the change set under "
    "review — do NOT change directories and write NO files. Emit ONLY the "
    "terminal task_result envelope matching the provided json-schema: status "
    "'done' with a SINGLE summary artifact (you write no files, so give it a "
    "synthetic path such as `review-noop.placeholder` — this path is a schema "
    "placeholder for the read-only review envelope: you write NO file and no "
    "consumer reads it; your verdict lives entirely in `notes_md`, and the "
    "envelope schema merely rejects a `done` with empty artifacts) and your "
    "verdict/finding lines in `notes_md`. "
    "The envelope is your sole output channel — do not narrate."
)

# The host-mode terminal-envelope instruction appended to a FIX (writer) brief.
# A fix worker writes the file in its OWN carved worktree (its cwd) and emits a
# `done` envelope on success (with one artifact per file written), or `blocked`
# with a one-line notes_md naming the obstacle (the host analog of the team-mode
# `ABANDON:` prefix). The engine's false-`done` guard DISCARDS a `done` whose
# declared write did not change vs HEAD, so a fix that touched nothing is caught.
_FIX_TERMINAL_RULE = (
    "Apply the fix to the file on disk in your CURRENT working directory (your "
    "task's isolated worktree — use bare relative paths, do not change "
    "directories), then emit ONLY the terminal task_result envelope matching the "
    "provided json-schema: status 'done' (with one artifact per file you wrote) "
    "on success, or status 'blocked' with a one-line notes_md naming the obstacle "
    "if the fix cannot be applied. The envelope is your sole output channel — do "
    "not narrate. Do nothing else."
)


# ── Task-dict builders (orchestrator-side; NO `reviews` key — engine stays single
#    dispatch per round) ──────────────────────────────────────────────────────


def build_review_tasks(
    reviewers: Sequence[str],
    action_items: Sequence[Mapping[str, Any]],
    impl_tasks: Sequence[Mapping[str, Any]],
    *,
    iter_n: int,
    parallel_group: int = 0,
) -> list[dict[str, Any]]:
    """Round-1 (Star-1) BROADCAST review tasks — one per reviewer.

    USER DECISION 1 (parity): every reviewer reviews the FULL written-file set
    (NOT round-robin) — so each task's ``reads`` is the SORTED UNION of every
    Phase-4 impl task's ``writes``. The tasks are READ-ONLY (``writes=[]``) so
    the engine runs them in the shared base clone (C1 — no worktree carved for a
    non-writer) and EXEMPTS them from the false-`done` guard.

    ``depends_on`` is EMPTY. In the single-window design the orchestrator
    dispatches the Phase-4 impl wave and THIS review round as SEPARATE
    ``run_host_pipeline_for_project`` calls, so by the time the review round is
    dispatched the impl tasks have already run-and-merged into the shared clone —
    they are NOT in the review dispatch's task list. Declaring ``depends_on`` on
    them would trip the engine's ``OrphanDepsError`` (a dep referencing a task
    absent from the CURRENT dispatch). The impl→review ordering is enforced by the
    SEQUENTIAL dispatch, not by an in-dispatch edge. ``impl_tasks`` is still read
    for the broadcast ``reads`` union. No ``reviews`` key is set: that keeps the
    engine's derived ``review_pairing`` EMPTY (kaizen owns the loop). ``task_id``
    is ``R{iter_n}-{reviewer_idx}`` — globally unique per reviewer per iteration
    (the finding-id re-stamp in :func:`_collect_review_findings` makes the
    per-FINDING ids unique too).
    """
    union_reads = sorted({w for t in impl_tasks for w in (t.get("writes") or [])})
    tasks: list[dict[str, Any]] = []
    for idx, reviewer in enumerate(reviewers):
        tasks.append(
            {
                "task_id": f"R{iter_n}-{idx}",
                "parallel_group": parallel_group,
                "depends_on": [],
                "writes": [],
                "reads": list(union_reads),
                "assigned_persona": reviewer,
                "phase": _REVIEW_PHASE,
                # NO "reviews" key — see the module-level decision note.
            }
        )
    return tasks


def build_mesh_tasks(
    reviewers: Sequence[str],
    action_items: Sequence[Mapping[str, Any]],
    r1_findings_by_reviewer: Mapping[str, Sequence[Finding]],
    impl_tasks: Sequence[Mapping[str, Any]],
    *,
    iter_n: int,
    parallel_group: int = 0,
) -> list[dict[str, Any]]:
    """Round-2 (Mesh) cross-confirmation tasks — one per reviewer.

    Same READ-ONLY shape as :func:`build_review_tasks`, INCLUDING ``reads``:
    every mesh task reads the SORTED UNION of all impl WRITES (the actual merged
    change set), in parity with round 1. ``task_id`` is
    ``M{iter_n}-{reviewer_idx}``. The PEER findings each reviewer cross-checks
    ride via the BRIEFING CLOSURE (``_make_mesh_briefing_for``), NOT in the task
    dict — the dict carries only routing/identity fields the engine consumes. The
    closure looks up ``M{iter_n}-{idx}`` → that reviewer's peer-finding set.

    ``reads`` MUST come from ``impl_tasks`` writes, NOT from the round-1 findings'
    file-references: a reviewer may flag a SUGGESTED file that does not yet exist
    (e.g. a missing test), and feeding such a ref into ``reads`` makes the mesh
    DAG declare an unsatisfiable read → the engine's reads-satisfiable gate
    (``validate_dag`` gate 3) rejects the whole DAG. The impl writes ARE
    satisfiable because the mesh dispatch passes the augmented ``review_existing``
    (base set plus impl writes; see ``_review_dispatch_round`` / ``round_existing``).

    ``r1_findings_by_reviewer`` documents the round-2 input contract (the per-task
    peer map is owned by the briefing closure); it does NOT feed ``reads``. No
    ``reviews`` key.
    """
    # `reads` parity with round 1: the reviewer re-inspects the same merged set —
    # i.e. the impl WRITES. NOT the findings' file-refs (a flagged suggested file
    # may not exist → would trip the engine's reads-satisfiable gate).
    union_reads = sorted({w for t in impl_tasks for w in (t.get("writes") or [])})
    tasks: list[dict[str, Any]] = []
    for idx, reviewer in enumerate(reviewers):
        tasks.append(
            {
                "task_id": f"M{iter_n}-{idx}",
                "parallel_group": parallel_group,
                "depends_on": [],
                "writes": [],
                "reads": list(union_reads),
                "assigned_persona": reviewer,
                "phase": _REVIEW_PHASE,
            }
        )
    return tasks


def build_fix_tasks(
    coalesced_by_file: Mapping[str, Sequence[Finding]],
    file_to_owner: Mapping[str, str],
    pm: str,
    *,
    iter_n: int,
    parallel_group: int = 0,
) -> list[dict[str, Any]]:
    """Fix-round WRITER tasks — ONE per file (same-file findings coalesced).

    Per SKILL Phase 5b' the IMPLEMENTER (the file's Phase-4 owner), NOT the
    reviewer who flagged it, fixes a finding. The owner is resolved via the
    reused :func:`scripts.team_executor._find_owner_for_finding` over the file's
    representative finding (any same-file finding routes to the same owner — they
    share a file → share an owner). ``writes=[file]`` makes the task a WRITER, so
    the engine carves it an isolated worktree and the false-`done` guard applies.
    ``depends_on=[]`` (the fixes for one round are write-disjoint by file).
    ``task_id`` is ``FIX{iter_n}-{file_idx}``. No ``reviews`` key.

    Files are iterated in SORTED order so the ``file_idx`` (and thus the task_id)
    is deterministic regardless of the input mapping's iteration order.
    """
    tasks: list[dict[str, Any]] = []
    for file_idx, file in enumerate(sorted(coalesced_by_file)):
        findings = list(coalesced_by_file[file])
        rep = findings[0]
        owner = _find_owner_for_finding(rep, dict(file_to_owner), pm)
        tasks.append(
            {
                "task_id": f"FIX{iter_n}-{file_idx}",
                "parallel_group": parallel_group,
                "depends_on": [],
                "writes": [file],
                "reads": [],
                "assigned_persona": owner,
                "phase": _FIX_PHASE,
            }
        )
    return tasks


def build_pm_task(
    blockers: Sequence[Finding],
    pm: str,
    *,
    iter_n: int,
    parallel_group: int = 0,
) -> dict[str, Any]:
    """The PM-acceptance gate as a READ-ONLY (``writes=[]``) dispatched task.

    The PM decides whether the round's surviving blocker/major findings are
    acceptable out-of-scope for THIS cycle (a legitimate fix-loop exit per SKILL)
    or whether to keep iterating. Read-only (false-`done`-exempt — the PM writes
    nothing). ``phase="review"`` resolves to opus through the same model key as the
    reviewers. ``task_id`` is ``PM{iter_n}``. No ``reviews`` key.
    """
    return {
        "task_id": f"PM{iter_n}",
        "parallel_group": parallel_group,
        "depends_on": [],
        "writes": [],
        "reads": [],
        "assigned_persona": pm,
        "phase": _REVIEW_PHASE,
    }


# ── Briefing-closure factories (pre-bound BEFORE the engine window) ──────────


def _make_review_briefing_for(
    items_by_id: Mapping[str, Mapping[str, Any]],
    action_items: Sequence[Mapping[str, Any]],
    phase_5b_prime_reviewer: Callable[..., str],
    prior_findings: Sequence[Finding] | None,
    teammate_reply_rule: str,
) -> Callable[[Mapping[str, Any], int], str]:
    """Build a round-1 reviewer ``briefing_for(task, attempt) -> str`` closure.

    ALL kaizen references are PRE-BOUND arguments captured BEFORE the engine
    window: the kaizen template fn, the (immutable) Action-Items list, the
    carry-forward prior-findings list, and the F7 trailer to strip. The closure
    does NO ``scripts.*`` import at call time. ``attempt`` is ignored for content
    (the prompt is iteration-stamped, not attempt-stamped). The F7/terse/reply-
    format trailer is cut via :func:`_strip_f7_trailer` and replaced by the host
    read-only terminal rule (which includes the C1 `git diff` instruction).

    The prior-findings carry-forward is the SAME ``prior_findings`` the team-mode
    loop passes (iteration 2+ reviewers do incremental review). It is fixed for
    THIS iteration's closure — the next iteration builds a fresh closure with that
    iteration's survivors.
    """
    trailer = teammate_reply_rule.strip()
    items = [dict(i) for i in action_items]
    prior = list(prior_findings) if prior_findings else None

    def briefing_for(task: Mapping[str, Any], attempt: int) -> str:
        iter_n = int(str(task["task_id"]).split("-", 1)[0][1:])  # "R{iter}-{idx}"
        rendered = phase_5b_prime_reviewer(
            iter_n=iter_n,
            action_items=items,
            prior_findings=prior,
        )
        body = _strip_f7_trailer(rendered, trailer)
        return f"{body}\n\n{_REVIEW_TERMINAL_RULE}"

    return briefing_for


def _make_mesh_briefing_for(
    items_by_id: Mapping[str, Mapping[str, Any]],
    action_items: Sequence[Mapping[str, Any]],
    peer_findings_by_task_id: Mapping[str, Sequence[Finding]],
    phase_5b_prime_reviewer_mesh: Callable[..., str],
    teammate_reply_rule: str,
) -> Callable[[Mapping[str, Any], int], str]:
    """Build a round-2 MESH ``briefing_for(task, attempt) -> str`` closure.

    ``peer_findings_by_task_id`` maps each mesh task_id (``M{iter}-{idx}``) to the
    PEER findings that reviewer must cross-check — i.e. the round-1 set MINUS that
    reviewer's OWN findings (the caller computes the exclusion). The closure looks
    the addressed task up and renders the mesh template with exactly that peer set,
    so reviewer A never sees A's own findings (no self-leak) and DOES see B's/C's
    (the cross-confirmation point). All refs pre-bound; no ``scripts.*`` at call
    time.
    """
    trailer = teammate_reply_rule.strip()
    items = [dict(i) for i in action_items]
    # Snapshot the peer map by value (lists) so a later mutation of the source
    # cannot retro-change a built closure's peer set.
    peer_map = {tid: list(findings) for tid, findings in peer_findings_by_task_id.items()}

    def briefing_for(task: Mapping[str, Any], attempt: int) -> str:
        tid = str(task["task_id"])
        iter_n = int(tid.split("-", 1)[0][1:])  # "M{iter}-{idx}"
        peer_findings = peer_map.get(tid, [])
        rendered = phase_5b_prime_reviewer_mesh(
            iter_n=iter_n,
            action_items=items,
            peer_findings=peer_findings,
        )
        body = _strip_f7_trailer(rendered, trailer)
        return f"{body}\n\n{_REVIEW_TERMINAL_RULE}"

    return briefing_for


def _make_pm_briefing_for(
    blockers_by_task_id: Mapping[str, Sequence[Finding]],
    phase_5b_prime_pm_acceptance: Callable[..., str],
    teammate_reply_rule: str,
    peer_unconfirmed_ids: set[str] | None = None,
) -> Callable[[Mapping[str, Any], int], str]:
    """Build a PM-acceptance ``briefing_for(task, attempt) -> str`` closure.

    ``blockers_by_task_id`` maps each PM task_id (``PM{iter}``) to that round's
    surviving blocker/major findings. The closure renders the UNCHANGED PM template
    over them, strips F7, and appends the host read-only terminal rule. All refs
    pre-bound; no ``scripts.*`` at call time.

    ``peer_unconfirmed_ids`` (M8a-2c LOW-1) is the set of blocker/major
    finding_ids that survived without any peer cross-confirm (from
    :func:`_consolidate_mesh`'s side-map). It is SNAPSHOT-BY-VALUE here (copied
    into a frozen local set) so the closure references the immutable snapshot,
    not a later-mutated caller object — the same pre-binding discipline as
    ``blockers_map``. Passed straight into the template as a plain kwarg.
    """
    trailer = teammate_reply_rule.strip()
    blockers_map = {tid: list(fs) for tid, fs in blockers_by_task_id.items()}
    unconfirmed_snapshot = set(peer_unconfirmed_ids or set())

    def briefing_for(task: Mapping[str, Any], attempt: int) -> str:
        tid = str(task["task_id"])
        iter_n = int(tid[2:])  # "PM{iter}"
        blockers = blockers_map.get(tid, [])
        rendered = phase_5b_prime_pm_acceptance(
            findings=blockers, iter_n=iter_n, peer_unconfirmed_ids=unconfirmed_snapshot
        )
        body = _strip_f7_trailer(rendered, trailer)
        return f"{body}\n\n{_REVIEW_TERMINAL_RULE}"

    return briefing_for


def _make_fix_briefing_for(
    finding_by_task_id: Mapping[str, Finding],
    phase_5b_prime_fix: Callable[..., str],
    teammate_reply_rule: str,
) -> Callable[[Mapping[str, Any], int], str]:
    """Build a FIX ``briefing_for(task, attempt) -> str`` closure.

    ``finding_by_task_id`` maps each fix task_id (``FIX{iter}-{file_idx}``) to the
    REPRESENTATIVE finding for that file. The closure renders the UNCHANGED fix
    template over it, strips F7, and appends the host WRITER terminal rule (write
    in cwd → emit `done`/`blocked` envelope). All refs pre-bound; no ``scripts.*``
    at call time.
    """
    trailer = teammate_reply_rule.strip()
    finding_map = dict(finding_by_task_id)

    def briefing_for(task: Mapping[str, Any], attempt: int) -> str:
        tid = str(task["task_id"])
        finding = finding_map[tid]
        rendered = phase_5b_prime_fix(finding=finding)
        body = _strip_f7_trailer(rendered, trailer)
        return f"{body}\n\n{_FIX_TERMINAL_RULE}"

    return briefing_for


# ── Round-1 collection + mesh parse + consolidation (pure, orchestrator-side) ─


def _collect_review_findings(
    reviewers: Sequence[str],
    results: Sequence[Mapping[str, Any]] | Sequence[Any],
    *,
    iter_n: int,
    is_failed_attempt: Callable[[Any], bool],
) -> list[Finding]:
    """Parse round-1 reviewer envelopes into globally-unique :class:`Finding`s.

    STRICT zip(reviewers, results) — the round-1 pipeline dispatched exactly one
    task per reviewer in ``R{iter_n}-{idx}`` order (all ``parallel_group=0``), and
    the engine returns results in ``(parallel_group, task_id)`` order, which for
    single-digit ``idx`` IS reviewer order.

    SAFETY (P2/F9 — must NOT collapse): a reviewer whose envelope is a failed-
    attempt sentinel OR carries a non-``done`` status is a SILENT reviewer — the
    caller HARD-abandons (review/`review_unrecoverable`). Soft-skipping it would
    ship an unreviewed change. This function signals that by raising
    :class:`_ReviewHardAbandon`; the loop converts it to the abandonment dict.

    Findings are parsed from ``result["notes_md"]`` (TOP-LEVEL on the validated
    envelope — R2) via the reused byte-sensitive
    :func:`scripts.team_executor._parse_reviewer_response`, then the per-reviewer
    ``finding_id`` is RE-STAMPED to ``R{iter_n}-{reviewer_idx}-{k}`` so two
    reviewers' first findings get DISTINCT ids (the parser alone would mint
    ``R{iter_n}-1`` for BOTH — a collision that would drop one in the attribution
    map). ``Finding`` is frozen, so the re-stamp reconstructs via ``Finding(...)``
    (its ``__post_init__`` severity validation stays fail-loud).
    """
    out: list[Finding] = []
    for idx, (reviewer, result) in enumerate(zip(reviewers, results, strict=True)):
        if is_failed_attempt(result):
            raise _ReviewHardAbandon(
                f"round-{iter_n} reviewer {reviewer!r} (R{iter_n}-{idx}) "
                f"returned a failed-attempt sentinel"
            )
        status = result.get("status") if isinstance(result, Mapping) else None
        if status != "done":
            raise _ReviewHardAbandon(
                f"round-{iter_n} reviewer {reviewer!r} (R{iter_n}-{idx}) "
                f"returned status={status!r} (expected 'done')"
            )
        notes = result.get("notes_md") if isinstance(result, Mapping) else None
        parsed = _parse_reviewer_response(notes or "", reviewer, prefix=f"R{iter_n}")
        for k, f in enumerate(parsed, start=1):
            out.append(
                Finding(
                    finding_id=f"R{iter_n}-{idx}-{k}",
                    reviewer=f.reviewer,
                    severity=f.severity,
                    finding=f.finding,
                    file_line=f.file_line,
                )
            )
    return out


# A recognized mesh verdict line: `CONFIRM <id>` | `RETRACT <id>` |
# `ESCALATE <id> <severity>` (one per line). The id token is the finding id
# (`R{iter}-{idx}-{k}`); the severity token (ESCALATE only) is one of the four.
_MESH_VERDICT_RE = re.compile(
    r"^\s*(?P<verb>CONFIRM|RETRACT|ESCALATE)\s+(?P<id>\S+)"
    r"(?:\s+(?P<sev>blocker|major|minor|nit))?\s*$"
)


def _parse_mesh_response(
    resp: str,
    reviewer: str,
    prefix: str,
) -> tuple[dict[str, Any], list[Finding]]:
    """Parse ONE reviewer's round-2 mesh reply.

    Returns ``(verdicts, net_new_findings)`` where ``verdicts`` maps
    ``finding_id -> "CONFIRM" | "RETRACT" | ("ESCALATE", severity)`` and
    ``net_new_findings`` are findings the reviewer raised that no peer had (parsed
    via the reused :func:`_parse_reviewer_response` over the raw reply, prefixed
    ``{prefix}-mesh-...`` by the caller's re-stamp).

    An UNRECOGNIZED verdict line is IGNORED (treated as a non-confirm — silence is
    not confirmation). BUT a reply that parses to ZERO recognized verdict lines AND
    zero finding lines is MALFORMED — the caller HARD-abandons it (C4 strict
    posture: a malformed/zero-verdict mesh reply is treated like a silent reviewer,
    never as a silent no-confirm that could let an unreviewed change ship).

    An ``ESCALATE <id>`` with a MISSING/invalid severity token does NOT match
    ``_MESH_VERDICT_RE`` (the severity group is required for an escalate to be
    recognized) → that line is ignored (non-confirm), not a silent demote.
    """
    verdicts: dict[str, Any] = {}
    n_verdict_lines = 0
    for line in (resp or "").splitlines():
        m = _MESH_VERDICT_RE.match(line)
        if m is None:
            continue
        verb = m.group("verb")
        fid = m.group("id")
        if verb == "ESCALATE":
            sev = m.group("sev")
            if sev is None:
                # An ESCALATE without a severity token is not actionable —
                # ignore it (do not silently demote / pick a default).
                continue
            verdicts[fid] = ("ESCALATE", sev)
        else:
            verdicts[fid] = verb
        n_verdict_lines += 1
    # Net-new findings: the reviewer may add issues no peer raised. The raw reply
    # is parsed for finding lines (the verdict lines do not match the finding
    # grammar, so they are not double-counted as findings).
    net_new = _parse_reviewer_response(resp or "", reviewer, prefix=prefix)
    # MALFORMED / zero-signal reply → caller HARD-abandons. We return a sentinel
    # (empty verdicts AND empty net_new) and let the caller decide, so this stays a
    # pure parser; but we record whether ANY recognized line was seen via the
    # combined emptiness check the caller performs.
    return verdicts, net_new


def _consolidate_mesh(
    r1_findings: Sequence[Finding],
    mesh_verdicts_by_reviewer: Mapping[str, Mapping[str, Any]],
    mesh_net_new: Sequence[Finding],
    *,
    n_reviewers: int,
) -> tuple[list[Finding], dict[str, bool]]:
    """Star-2 consolidation (PURE) — apply the C4 severity-gated weeding rule.

    For each round-1 finding ``f`` (keyed by its globally-unique ``finding_id``):

    * **self-retract** — if ANY reviewer issued ``RETRACT <f.id>`` → DROP (any
      severity). (Conservatively any reviewer's retract drops it; the author is
      the common case but the id is globally unique so only the author's own
      finding carries that id.)
    * **escalate** — the MAX asserted ``ESCALATE <f.id> <sev>`` across peers RAISES
      ``f.severity`` (never demotes — a lower escalate target is ignored).
      Reconstructed via ``Finding(...)`` so ``__post_init__`` stays fail-loud.
    * **peer-confirm weed (SEVERITY-GATED — F9 integrity, C4):**
        - ``blocker`` / ``major``: RETAINED even with ZERO peer confirms; if
          unconfirmed it is flagged ``peer_unconfirmed=True`` in the RETURNED
          side-map. NEVER silently dropped — it stays in ``survivors`` so it
          counts as a blocker, drives a fix, and hits the PM gate regardless of
          the flag. (M8a-2c LOW-1: the loop now BINDS this side-map and surfaces
          the flag NEUTRALLY in the PM briefing + the convergence summary on
          exhaustion; it does NOT soften the PM toward acceptance.)
        - ``minor`` / ``nit``: DROPPED unless confirmed by >=1 peer.

    ``n_reviewers == 1`` is the VACUOUS-QUORUM case (spec §2.3 step 4): there are
    no peers, the mesh round is SKIPPED, so there are no verdicts at all. With one
    reviewer NOTHING is weeded — EVERY round-1 finding survives at its original
    severity (the minor/nit peer-confirm gate is a multi-reviewer rule; applying it
    to a sole reviewer would silently empty the set even though that reviewer DID
    flag the issue). This function takes the ``n_reviewers == 1`` branch explicitly
    rather than relying on the confirm-count incidentally being 0.

    Net-new round-2 findings are admitted at face value and ride forward.

    Returns ``(survivors, peer_unconfirmed_map)``. ``Finding`` is frozen so the
    side-map (not a mutated field) carries the ``peer_unconfirmed`` flag.
    """
    if n_reviewers == 1:
        # Vacuous quorum — no peers, mesh skipped. Keep every round-1 finding
        # verbatim (no self-retract verdicts exist either; mesh did not run).
        # Net-new is empty in this path (no mesh round produced any), but we
        # extend defensively for shape parity.
        return list(r1_findings) + list(mesh_net_new), {}
    # Flatten verdicts across reviewers. self-retract = any RETRACT for the id;
    # escalate target = max severity asserted; confirm count = number of CONFIRM
    # verdicts (escalate also counts as a confirm — a peer asserting a HIGHER
    # severity has plainly confirmed the finding is real).
    retracted: set[str] = set()
    escalate_to: dict[str, str] = {}
    confirm_count: dict[str, int] = {}
    for _reviewer, verdicts in mesh_verdicts_by_reviewer.items():
        for fid, verdict in verdicts.items():
            if verdict == "RETRACT":
                retracted.add(fid)
            elif verdict == "CONFIRM":
                confirm_count[fid] = confirm_count.get(fid, 0) + 1
            elif isinstance(verdict, tuple) and verdict and verdict[0] == "ESCALATE":
                sev = verdict[1]
                confirm_count[fid] = confirm_count.get(fid, 0) + 1
                cur = escalate_to.get(fid)
                if cur is None or _severity_rank(sev) > _severity_rank(cur):
                    escalate_to[fid] = sev

    survivors: list[Finding] = []
    peer_unconfirmed: dict[str, bool] = {}
    for f in r1_findings:
        fid = f.finding_id
        if fid in retracted:
            continue  # self-retract drops any severity.
        # Apply escalation (raise only — _consolidate never demotes).
        eff_severity = f.severity
        target = escalate_to.get(fid)
        if target is not None and _severity_rank(target) > _severity_rank(f.severity):
            eff_severity = target
        confirms = confirm_count.get(fid, 0)
        if eff_severity in _BLOCKING_SEVERITIES:
            # blocker/major: ALWAYS retained; flag if no peer confirmed it.
            survivor = (
                f if eff_severity == f.severity else _reconstruct_with_severity(f, eff_severity)
            )
            survivors.append(survivor)
            if confirms == 0:
                peer_unconfirmed[fid] = True
        else:
            # minor/nit: weeded unless a peer confirmed it.
            if confirms >= 1:
                survivor = (
                    f if eff_severity == f.severity else _reconstruct_with_severity(f, eff_severity)
                )
                survivors.append(survivor)
    # Net-new findings ride forward at face value.
    survivors.extend(mesh_net_new)
    return survivors, peer_unconfirmed


# Severity ordering for the escalate "raise only" rule (higher index = higher).
_SEVERITY_ORDER: tuple[str, ...] = ("nit", "minor", "major", "blocker")


def _severity_rank(severity: str) -> int:
    """Ordinal rank of a severity (higher = more severe). FAIL LOUD on unknown —
    an unrecognized severity would otherwise sort as -1 and silently never
    escalate (or always demote)."""
    try:
        return _SEVERITY_ORDER.index(severity)
    except ValueError:
        raise ValueError(f"unknown severity {severity!r}; known: {list(_SEVERITY_ORDER)}") from None


def _reconstruct_with_severity(f: Finding, severity: str) -> Finding:
    """Rebuild ``f`` with a new ``severity`` (``Finding`` is frozen). Goes through
    ``Finding(...)`` so ``__post_init__`` validates the new severity fail-loud."""
    return Finding(
        finding_id=f.finding_id,
        reviewer=f.reviewer,
        severity=severity,
        finding=f.finding,
        file_line=f.file_line,
    )


class _ReviewHardAbandon(RuntimeError):
    """Internal control-flow signal: a review/mesh round hit a HARD-abandon
    condition (a silent/failed reviewer, or a malformed/zero-verdict mesh reply).
    The loop catches it and builds the canonical ``review_unrecoverable``
    abandonment dict. NEVER leaks out of :func:`_run_review_fix_loop`."""


def _coalesce_blockers_by_file(blockers: Sequence[Finding]) -> dict[str, list[Finding]]:
    """Group blocker/major findings by their file (the ``file_line`` path part).

    Per SKILL the fix round dispatches ONE writer per file (not one per finding) —
    several findings on the same file are coalesced so they are fixed in a single
    write-isolated worktree. The representative finding (first in input order)
    routes the owner; the rest are listed in the fix briefing's prose.
    """
    by_file: dict[str, list[Finding]] = {}
    for f in blockers:
        raw = f.file_line or ""
        file = raw.split(":", 1)[0] if ":" in raw else raw
        by_file.setdefault(file, []).append(f)
    return by_file


def _run_review_fix_loop(
    *,
    reviewers: Sequence[str],
    action_items: Sequence[Mapping[str, Any]],
    impl_tasks: Sequence[Mapping[str, Any]],
    file_to_owner: Mapping[str, str],
    pm: str,
    subject: str | None,
    participants: Sequence[str],
    dispatch_round: Callable[..., list[Any]],
    review_briefing_factory: Callable[
        [Sequence[Finding] | None], Callable[[Mapping[str, Any], int], str]
    ],
    mesh_briefing_factory: Callable[
        [Mapping[str, Sequence[Finding]]], Callable[[Mapping[str, Any], int], str]
    ],
    pm_briefing_factory: Callable[..., Callable[[Mapping[str, Any], int], str]],
    fix_briefing_factory: Callable[
        [Mapping[str, Finding]], Callable[[Mapping[str, Any], int], str]
    ],
    is_failed_attempt: Callable[[Any], bool],
) -> dict[str, Any] | None:
    """Orchestrator-owned Phase 5b' review→fix loop (re-homed Star→Mesh→Star).

    ``FixLoopState`` is the single source of truth for the iteration counter +
    history. Returns a ``review_unrecoverable`` abandonment dict (to be folded into
    the cycle outcome) OR ``None`` on a clean exit (zero blockers / PM-accept).

    Each round's engine dispatch goes through ``dispatch_round(tasks, briefing_for)``
    — a closure supplied by :func:`host_cycle_executor` that runs ONE
    ``run_host_pipeline_for_project`` call INSIDE the already-open
    ``atelier_engine`` window (single-window design, R4/R5). The CONSOLIDATION,
    PARSE, and CONSENSUS run OUT of the engine call (pure Python over the returned
    results), exactly as the spec's §2.2 requires.

    The ``*_briefing_factory`` callables build the per-round briefing closure given
    that round's data (prior findings / peer-finding map / blocker map / fix-finding
    map). Each factory was itself pre-bound to the module-level template fns BEFORE
    the window (no ``scripts.*`` import at call time).
    """
    state = FixLoopState()
    n_reviewers = len(reviewers)
    try:
        while True:
            iter_n = start_iteration(state)
            prior = state.history[-1] if state.history else None

            # ── ROUND 1 (Star-1, broadcast) ──────────────────────────────────
            r1_tasks = build_review_tasks(reviewers, action_items, impl_tasks, iter_n=iter_n)
            r1_briefing = review_briefing_factory(prior)
            r1_results = dispatch_round(r1_tasks, r1_briefing)
            r1_findings = _collect_review_findings(
                reviewers, r1_results, iter_n=iter_n, is_failed_attempt=is_failed_attempt
            )

            # ── Skip-mesh fast paths ─────────────────────────────────────────
            # `peer_unconfirmed` is the LATEST round's blocker/major-without-peer-
            # confirm side-map from `_consolidate_mesh` (LOW-1). It flows to the PM
            # briefing + convergence summary; previously discarded here.
            peer_unconfirmed: dict[str, bool] = {}
            if not r1_findings:
                # Zero round-1 findings → no mesh dispatch, clean exit.
                survivors: list[Finding] = []
                peer_unconfirmed = {}
                record_findings(state, survivors)
            elif n_reviewers == 1:
                # Single reviewer → mesh is vacuous; consolidate with no peers (the
                # map is `{}` by construction — a sole reviewer has no peers).
                survivors, peer_unconfirmed = _consolidate_mesh(r1_findings, {}, [], n_reviewers=1)
                record_findings(state, survivors)
            else:
                # ── ROUND 2 (Mesh) ───────────────────────────────────────────
                # Each reviewer cross-checks its PEERS' findings (own findings
                # excluded). A reviewer whose peers found NOTHING has nothing to
                # cross-check, so it is NOT dispatched a mesh task — an empty peer
                # set would be rejected by the mesh template AND would spuriously
                # trip the zero-signal HARD-abandon below. When all round-1
                # findings come from ONE reviewer, only the OTHER reviewer(s) are
                # polled — exactly the cross-confirmation we want.
                findings_by_reviewer_idx: dict[int, list[Finding]] = {}
                for f in r1_findings:
                    # finding_id is "R{iter}-{reviewer_idx}-{k}".
                    rev_idx = int(f.finding_id.split("-")[1])
                    findings_by_reviewer_idx.setdefault(rev_idx, []).append(f)

                peer_findings_by_task_id: dict[str, list[Finding]] = {}
                dispatch_idxs: list[int] = []
                for idx in range(n_reviewers):
                    peers = [
                        f
                        for other_idx, fs in findings_by_reviewer_idx.items()
                        if other_idx != idx
                        for f in fs
                    ]
                    if not peers:
                        continue  # nothing for this reviewer to cross-check
                    peer_findings_by_task_id[f"M{iter_n}-{idx}"] = peers
                    dispatch_idxs.append(idx)

                # build_mesh_tasks emits one task per reviewer (task_id
                # M{iter}-{idx}); dispatch ONLY the subset that has peers to review.
                all_mesh_tasks = build_mesh_tasks(
                    reviewers,
                    action_items,
                    {reviewers[i]: findings_by_reviewer_idx.get(i, []) for i in range(n_reviewers)},
                    impl_tasks,
                    iter_n=iter_n,
                )
                mesh_tasks = [all_mesh_tasks[idx] for idx in dispatch_idxs]
                mesh_briefing = mesh_briefing_factory(peer_findings_by_task_id)
                mesh_results = dispatch_round(mesh_tasks, mesh_briefing)

                # STRICT zip over the DISPATCHED subset; HARD-abandon on any
                # failed/non-`done` envelope, and on a polled reviewer whose reply
                # parses to ZERO recognized lines (it has >=1 peer finding to
                # CONFIRM/RETRACT, so a zero-signal reply is malformed).
                mesh_verdicts_by_reviewer: dict[str, dict[str, Any]] = {}
                mesh_net_new: list[Finding] = []
                for idx, result in zip(dispatch_idxs, mesh_results, strict=True):
                    reviewer = reviewers[idx]
                    if is_failed_attempt(result):
                        raise _ReviewHardAbandon(
                            f"round-{iter_n} mesh reviewer {reviewer!r} (M{iter_n}-{idx}) "
                            f"returned a failed-attempt sentinel"
                        )
                    status = result.get("status") if isinstance(result, Mapping) else None
                    if status != "done":
                        raise _ReviewHardAbandon(
                            f"round-{iter_n} mesh reviewer {reviewer!r} (M{iter_n}-{idx}) "
                            f"returned status={status!r} (expected 'done')"
                        )
                    notes = result.get("notes_md") if isinstance(result, Mapping) else None
                    verdicts, net_new = _parse_mesh_response(
                        notes or "", reviewer, prefix=f"R{iter_n}-mesh-{idx}"
                    )
                    if not verdicts and not net_new:
                        # Malformed / zero-signal reply → HARD abandon (C4).
                        raise _ReviewHardAbandon(
                            f"round-{iter_n} mesh reviewer {reviewer!r} (M{iter_n}-{idx}) "
                            f"reply parsed to ZERO recognized verdict/finding lines"
                        )
                    mesh_verdicts_by_reviewer[reviewer] = verdicts
                    mesh_net_new.extend(net_new)

                survivors, peer_unconfirmed = _consolidate_mesh(
                    r1_findings,
                    mesh_verdicts_by_reviewer,
                    mesh_net_new,
                    n_reviewers=n_reviewers,
                )
                record_findings(state, survivors)

            # ── Convergence check ────────────────────────────────────────────
            latest_blockers = [f for f in survivors if f.severity in _BLOCKING_SEVERITIES]
            if not latest_blockers:
                return None  # clean exit (zero blocking findings).

            # Blocker/major finding_ids that survived without any peer cross-
            # confirm (LOW-1) — restricted to this round's surviving blockers so
            # the PM briefing only marks lines it actually renders.
            unconfirmed_ids = {
                f.finding_id for f in latest_blockers if peer_unconfirmed.get(f.finding_id)
            }

            # ── PM-acceptance (read-only dispatched task) ────────────────────
            pm_task = build_pm_task(latest_blockers, pm, iter_n=iter_n)
            pm_briefing = pm_briefing_factory(
                {pm_task["task_id"]: latest_blockers}, unconfirmed_ids
            )
            pm_results = dispatch_round([pm_task], pm_briefing)
            pm_result = pm_results[0]
            pm_resp = pm_result.get("notes_md") if isinstance(pm_result, Mapping) else None
            # Verbatim team_executor.py:2381 — a literal ACCEPT prefix (after
            # strip + upper) is the ONLY clean-accept signal; everything else
            # (incl. an "ABANDON:" prefix) is a REJECT.
            pm_accepts = (pm_resp or "").strip().upper().startswith("ACCEPT")

            if not should_continue(state, pm_accepts_remaining=pm_accepts):
                if pm_accepts:
                    return None  # clean exit — PM ruled remaining issues acceptable.
                # MAX_ITERATIONS reached with blockers still present. Pass the
                # LATEST round's peer-unconfirmed map ONLY here (the exhaustion
                # site) — fix-failed + HARD-abandon below pass None.
                return build_abandonment_outcome(
                    state,
                    subject=subject,
                    participants=list(participants),
                    peer_unconfirmed=unconfirmed_ids,
                )

            # ── FIX round (writers, one per coalesced file) ──────────────────
            coalesced = _coalesce_blockers_by_file(latest_blockers)
            fix_tasks = build_fix_tasks(coalesced, file_to_owner, pm, iter_n=iter_n)
            # Map each fix task_id → its representative finding for the briefing.
            finding_by_task_id: dict[str, Finding] = {}
            for file_idx, file in enumerate(sorted(coalesced)):
                finding_by_task_id[f"FIX{iter_n}-{file_idx}"] = coalesced[file][0]
            fix_briefing = fix_briefing_factory(finding_by_task_id)
            fix_results = dispatch_round(fix_tasks, fix_briefing)
            # Any fix envelope failed / blocked → abandonment.
            for _fix_task, fix_result in zip(fix_tasks, fix_results, strict=True):
                if is_failed_attempt(fix_result):
                    return build_abandonment_outcome(
                        state, subject=subject, participants=list(participants)
                    )
                status = fix_result.get("status") if isinstance(fix_result, Mapping) else None
                if status != "done":
                    return build_abandonment_outcome(
                        state, subject=subject, participants=list(participants)
                    )
            # Loop back to round 1 for re-review of the fixes.
    except _ReviewHardAbandon as exc:
        # A silent/failed reviewer or a malformed/zero-verdict mesh reply →
        # canonical review_unrecoverable abandonment. Ensure `state.history` has
        # at least one entry so build_abandonment_outcome renders a stable shape.
        _log.warning("host Phase 5b' HARD abandon: %s", exc)
        if not state.history:
            record_findings(state, [])
        return build_abandonment_outcome(state, subject=subject, participants=list(participants))


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


def _run_ci_gate(
    clone_path: Path,
    test_command: str,
    ci_baseline: dict[str, dict] | None,
    *,
    subject: str | None,
    participants: Sequence[str],
) -> tuple[dict[str, dict] | None, dict[str, Any] | None]:
    """Run the post-Phase-4 CI-mirror gate (M8a-2c §2). REUSE — kaizen has it.

    Single post-Phase-4 gate (the engine runs the whole DAG in ONE call — there
    is no per-wave Python boundary here; per-wave CI is out of scope/future).
    Mirrors team mode's wave-boundary gate logic byte-for-byte via the shared
    `_diff_ci_results` / `_pick_highest_reason` helpers:

      * run ``run_ci_checks(clone, test_command)`` (honors KAIZEN_SKIP_CHECKS /
        KAIZEN_SKIP_PIP_AUDIT and skips missing binaries gracefully);
      * diff against the pre-window ``ci_baseline`` — pre-existing failures are
        LOGGED ONLY, never abandon (without this a target with debt abandons
        every cycle);
      * cycle-introduced failures → an abandonment dict (``phase_reached="test"``,
        the highest-severity per-check reason, detail naming BOTH lists, the four
        review-outcome keys = None for shape parity).

    Returns ``(last_ci_results, abandonment_or_None)``. ``last_ci_results`` is the
    gate's per-check dict (the caller parses its pytest pass-count for the commit
    message). The second element is None when the gate passes (or only pre-existing
    failures remain) → proceed to commit. Runs OUTSIDE the engine window.
    """
    all_passed, last_ci_results = run_ci_checks(clone_path, test_command)
    if all_passed:
        return last_ci_results, None
    cycle_introduced, pre_existing = _diff_ci_results(ci_baseline, last_ci_results)
    if pre_existing:
        # Pre-existing failures: log (structured logging is out of scope) but do
        # NOT abandon — they predate the cycle's edits.
        _log.warning(
            "host CI gate: ignoring pre-existing failures from baseline: %s",
            pre_existing,
        )
    if not cycle_introduced:
        # Every failure was pre-existing → the cycle is clean; proceed to commit.
        return last_ci_results, None
    # Cycle-introduced failures → abandon at the test phase, mapping the
    # highest-severity failed category to its per-CI-kind reason (parity with
    # team_executor — same `_pick_highest_reason`/`_CHECK_TO_REASON` taxonomy).
    reason = _pick_highest_reason(cycle_introduced)
    detail = (
        "host CI gate failed after Phase 4: "
        f"cycle-introduced={cycle_introduced}, pre-existing={pre_existing}"
    )
    return last_ci_results, {
        "status": "abandoned",
        "subject": subject,
        "participants": list(participants),
        "phase_reached": "test",
        "reason": reason,
        "detail": detail,
        "artifacts": [],
        "review_iteration_count": None,
        "unresolved_findings": None,
        "convergence_summary": None,
        "reviewer_attribution": None,
    }


def _review_roster_abandon(
    subject: str | None,
    participants: Sequence[str],
    detail: str,
) -> dict[str, Any]:
    """Abandonment dict for the pre-loop 'cannot select disjoint reviewers' case.

    Mirrors team mode's reviewer-selection abandon (``phase_reached="review"``,
    ``reason="other"``). Carries the four review-outcome keys as ``None`` (no fix
    loop ran), so the dict is key-compatible with :func:`build_abandonment_outcome`
    consumers (run.py / abandonment.py) and the parity contract.
    """
    return {
        "status": "abandoned",
        "subject": subject,
        "participants": list(participants),
        "phase_reached": "review",
        "reason": "other",
        "detail": detail,
        "artifacts": [],
        "review_iteration_count": None,
        "unresolved_findings": None,
        "convergence_summary": None,
        "reviewer_attribution": None,
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
    review: bool = True,
    cycle_n: int = 1,
    run_id: int | None = None,
    test_command: str = "pytest",
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
         gate). Capture the CI baseline (``run_ci_checks(clone, "true")``) here,
         BEFORE the window, so the post-Phase-4 gate can diff cycle-introduced
         failures from pre-existing debt (§2; mandatory).
      5. ``with atelier_engine(root) as host:`` — construct BudgetPool /
         ResultJournal / sandbox / worktree-factory / neutral run_mode IN-window
         (these are atelier's), then ``asyncio.run(run_host_pipeline_for_project(...))``
         ENTIRELY inside the window. On a clean Phase-4 success, the Phase 5b'
         review→fix loop runs in the SAME window (M8a-2b).
      6. OUTSIDE the window: interpret results → outcome dict, then SELF-CONTAINED
         finalization (M8a-2c) — CI-mirror gate, then commit the merged work via
         :func:`scripts.cycle_git.commit_cycle_and_sha` and stamp the real
         ``commit_sha`` + ``minutes_memex_slug`` (slug
         ``kaizen:cycle:{run_id}-{cycle_n}`` or ``kaizen:cycle:host-{cycle_n}``).

    The commit happens INSIDE this executor (before run.py inspects the outcome)
    so F3 holds with NO run.py change. The default ``journal_path`` lives OUTSIDE
    the clone (a tempdir this executor owns) so the commit's transient-dir strip
    cannot delete the journal mid-flight (§1A).

    ``runner`` defaults to ``None`` → atelier's ``real_cli_runner`` resolved
    in-window. Tests inject a ``FakeCliRunner`` (no real ``claude``).
    ``test_command`` is the target's CI test command (mirrored by the gate);
    ``cycle_n`` / ``run_id`` feed the commit message + Memex slug.
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
    from scripts.dispatch_templates import (
        TEAMMATE_REPLY_RULE,
        phase_4_implementer,
        phase_5b_prime_fix,
        phase_5b_prime_pm_acceptance,
        phase_5b_prime_reviewer,
        phase_5b_prime_reviewer_mesh,
    )

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

    # CRITICAL journal hazard (M8a-2c §1A): the post-Phase-4 commit calls
    # `commit_cycle → _strip_transient_dirs`, which rmtrees the clone's UNTRACKED
    # `clone/.ai/`. The old default journal path `clone/.ai/host-journal.json`
    # would therefore be DELETED out from under the still-open ResultJournal mid
    # commit. The default journal now lives OUTSIDE the clone — a DETERMINISTIC
    # sibling of the clone dir in its (ephemeral, gitignored experiment) parent —
    # so the commit's transient-dir strip can never touch it, it leaks no per-cycle
    # /tmp dir, and a test can assert it SURVIVES at its real location. An explicit
    # `journal_path` (tests / live e2e) is honored verbatim — callers are expected
    # to point it outside the clone too.
    if journal_path is not None:
        journal_file = Path(journal_path)
    else:
        journal_file = clone_path.parent / f"{clone_path.name}.host-journal.json"
    journal_file.parent.mkdir(parents=True, exist_ok=True)

    # ── CI baseline (M8a-2c §2 — MANDATORY) ─────────────────────────────────
    # Capture the target's CI state BEFORE the engine runs ANY Phase-4 work, so
    # the post-Phase-4 gate can tell "the cycle introduced this break" apart from
    # "the host arrived with this break." WITHOUT this baseline, a target with a
    # pre-existing ruff/bandit/pip-audit debt would abandon EVERY cycle (the
    # pre-F10 incident). `"true"` is the baseline test command (a no-op exit-0) so
    # the baseline captures lint/security/sca state, not a pre-cycle pytest run.
    # A baseline crash is non-fatal — log and proceed with baseline=None (every
    # fail then reads as cycle-introduced, the pre-F10 behavior). This runs
    # OUTSIDE the engine window (run_ci_checks is the kaizen helper, pre-bound).
    ci_baseline: dict[str, dict] | None = None
    try:
        _baseline_passed, ci_baseline = run_ci_checks(clone_path, "true")
    except Exception as baseline_exc:
        _log.warning("host CI baseline run failed: %s — proceeding without diff", baseline_exc)
        ci_baseline = None

    # Set on the engine-worktree-failure path (the in-window try/except below):
    # an engine WorktreeError is converted to a graceful abandon dict rather than
    # propagating as an uncaught traceback (M8b finding). Resolved OUTSIDE the
    # window with the SAME (highest) precedence as a review/Phase-4 abandon.
    worktree_abandon: dict[str, Any] | None = None
    # Bound inside the window (the dispatch + interpret + review loop run there);
    # initialized here so the post-window resolution can reference them even on the
    # WorktreeError short-circuit path (where the dispatch never assigned them).
    phase4_outcome: dict[str, Any] | None = None
    review_outcome: dict[str, Any] | None = None

    # ── The engine window. EVERYTHING atelier — BudgetPool, ResultJournal,
    #    sandbox, worktree factory, neutral run_mode, the runner default, AND
    #    the whole asyncio.run — happens here. ─────────────────────────────────
    with atelier_engine(atelier_root) as host:
        cli_dispatch = importlib.import_module("scripts.cli_dispatch")
        budget_pool_mod = importlib.import_module("scripts.budget_pool")
        result_journal_mod = importlib.import_module("scripts.result_journal")
        run_mode_mod = importlib.import_module("scripts.run_mode")

        # FOOTGUN (swap window): WorktreeError is an ATELIER class. INSIDE this
        # window `scripts.host_scheduler` resolves to ATELIER, so bind the class
        # HERE via importlib — a module-top `from scripts.host_scheduler import
        # WorktreeError` would resolve to KAIZEN, where the class does not exist.
        _WorktreeError = importlib.import_module("scripts.host_scheduler").WorktreeError

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

        # CRITICAL: every asyncio.run is INSIDE the window — the coroutine
        # touches atelier scripts.* throughout; closing early = ImportError.
        # `_dispatch_round` runs ONE pipeline call; it is reused for the Phase-4
        # wave AND every Phase-5b' review/mesh/PM/fix round (single-window design
        # — see _run_review_fix_loop). review_pairing is left unset, so the engine
        # NEVER runs its own nested review loop (kaizen owns the loop).
        def _dispatch_round(
            round_tasks: Sequence[Mapping[str, Any]],
            round_briefing: Callable[[Mapping[str, Any], int], str],
            *,
            round_existing: Sequence[str] | None = None,
        ) -> list[Any]:
            # `round_existing` overrides the dispatch's existing-file set. Phase-4
            # uses the original `existing`; the review/mesh rounds pass the
            # POST-MERGE set (original + every impl write) so a reviewer's
            # broadcast `reads` resolve as pre-existing (the producing impl tasks
            # are not in the review-only dispatch — declaring them via reads/deps
            # would trip validate_dag's reads-satisfiable / orphan-deps gates).
            return asyncio.run(
                host.run_host_pipeline_for_project(
                    round_tasks,
                    clone_dir=str(clone_path),
                    budget=budget,
                    journal=journal,
                    existing_files=sorted(existing)
                    if round_existing is None
                    else list(round_existing),
                    model_for=model_for,
                    briefing_for=round_briefing,
                    worktree_factory=wt,
                    runner=eff_runner,
                    sandbox_wrap=sandbox,
                    escalate_fn=escalate_fn,
                    run_mode=neutral_run_mode,
                    disallowed_tools=disallowed,
                )
            )

        is_failed_attempt = cli_dispatch.is_failed_attempt

        # NARROW catch around ALL engine dispatch in this window — the Phase-4
        # wave AND every Phase-5b' review/mesh/PM/fix round (each runs through
        # `_dispatch_round`, and the engine's EAGER worktree merge can fail from
        # ANY round). An engine WorktreeError (a worktree create/merge failure —
        # e.g. a CRLF-dirty base tree refusing `git merge --no-ff`, the M8b
        # finding) becomes a graceful kaizen abandon dict instead of an uncaught
        # traceback. The catch is ONLY `_WorktreeError` (bound in-window above):
        # kaizen's deliberate fail-loud ValueErrors (model_for / _severity_rank /
        # Finding) MUST still crash loudly — barred over-catch class (PR #110).
        try:
            results = _dispatch_round(tasks, briefing_for)

            # Phase-4 outcome (pure interpret, IN-window so the review loop can run
            # in the SAME window on success).
            phase4_outcome = _interpret_engine_results(
                results,
                tasks,
                participants=participants,
                subject=subject,
                is_failed_attempt=is_failed_attempt,
            )

            # ── Phase 5b' — independent review + fix loop (re-homed Star→Mesh→Star).
            #    Only on a clean Phase-4 success; mirrors team mode's ordering. ──
            if review and phase4_outcome.get("status") == "success":
                # Reviewer selection (§2.4) — VERBATIM parity with team_executor:
                # the disjoint pool excludes every implementer persona; <1 disjoint
                # role → abandon at the review phase.
                implementers = [str(item["owner"]) for item in action_items if item.get("owner")]
                disjoint_pool_size = len([r for r in roster if r not in set(implementers)])
                n_reviewers = min(3, disjoint_pool_size) if disjoint_pool_size > 0 else 0
                if n_reviewers < 1:
                    review_outcome = _review_roster_abandon(
                        subject,
                        participants,
                        "Cannot select any disjoint reviewer — roster too small "
                        f"({len(roster)} role(s), {len(set(implementers))} implementer(s)).",
                    )
                else:
                    try:
                        reviewers = select_reviewers(
                            list(roster),
                            implementers,
                            n=n_reviewers,
                            preferred_lenses=list(_REVIEWER_LENSES),
                        )
                    except InsufficientRosterError as exc:
                        review_outcome = _review_roster_abandon(
                            subject, participants, f"Cannot select disjoint reviewers: {exc}"
                        )
                    else:
                        # file → Phase-4 owner, for fix-task routing via the reused
                        # _find_owner_for_finding. Built OUTSIDE the engine call.
                        file_to_owner = {
                            path: str(item["owner"])
                            for item in action_items
                            if item.get("owner")
                            for path in (item.get("touches") or [])
                        }

                        # Briefing-closure FACTORIES — closed over the module-level
                        # kaizen template fns (imported before the window) + the
                        # immutable items/action_items. The loop calls each per
                        # round with that round's data; no scripts.* import at call
                        # time.
                        def _review_fac(prior):
                            return _make_review_briefing_for(
                                items_by_id,
                                action_items,
                                phase_5b_prime_reviewer,
                                prior,
                                TEAMMATE_REPLY_RULE,
                            )

                        def _mesh_fac(peer_map):
                            return _make_mesh_briefing_for(
                                items_by_id,
                                action_items,
                                peer_map,
                                phase_5b_prime_reviewer_mesh,
                                TEAMMATE_REPLY_RULE,
                            )

                        def _pm_fac(bmap, peer_unconfirmed_ids=None):
                            return _make_pm_briefing_for(
                                bmap,
                                phase_5b_prime_pm_acceptance,
                                TEAMMATE_REPLY_RULE,
                                peer_unconfirmed_ids=peer_unconfirmed_ids,
                            )

                        def _fix_fac(fmap):
                            return _make_fix_briefing_for(
                                fmap, phase_5b_prime_fix, TEAMMATE_REPLY_RULE
                            )

                        # Post-merge existing-file set: every Phase-4 impl write now
                        # lives in the shared clone. The review loop dispatches each
                        # round with THIS set so broadcast reviewer `reads` (the impl
                        # writes) satisfy the engine's reads-satisfiable gate.
                        review_existing = sorted(
                            set(existing) | {w for t in tasks for w in (t.get("writes") or [])}
                        )

                        def _review_dispatch_round(round_tasks, round_briefing):
                            return _dispatch_round(
                                round_tasks, round_briefing, round_existing=review_existing
                            )

                        review_outcome = _run_review_fix_loop(
                            reviewers=reviewers,
                            action_items=action_items,
                            impl_tasks=tasks,
                            file_to_owner=file_to_owner,
                            pm=pm,
                            subject=subject,
                            participants=participants,
                            dispatch_round=_review_dispatch_round,
                            review_briefing_factory=_review_fac,
                            mesh_briefing_factory=_mesh_fac,
                            pm_briefing_factory=_pm_fac,
                            fix_briefing_factory=_fix_fac,
                            is_failed_attempt=is_failed_attempt,
                        )
        except _WorktreeError as exc:
            # Engine worktree create/merge failure → graceful abandon (phase
            # 'implementation', reason 'other' — an infra/engine failure, NOT a
            # semantic no_consensus or a CI break). Carry the engine message in
            # detail; shape matches the other implementation-phase abandons.
            _log.warning("host engine WorktreeError → graceful abandon: %s", exc)
            worktree_abandon = {
                "status": "abandoned",
                "subject": subject,
                "participants": list(participants),
                "phase_reached": "implementation",
                "reason": "other",
                "detail": (
                    f"host engine worktree merge/create failed (atelier WorktreeError): {exc}"
                ),
                "artifacts": [],
            }

    # ── OUTSIDE the window — self-contained finalization (M8a-2c §1/§2). ────────
    # The engine window is closed; `scripts` is kaizen again, so the CI helpers,
    # `commit_cycle_and_sha`, and `parse_pytest_pass_count` (all pre-bound at
    # module top) run here. F3 is satisfied with NO run.py change: the commit
    # happens INSIDE host_cycle_executor, before run.py inspects the outcome — do
    # NOT move it to run.py.
    #
    # Resolution order (only ONE path commits):
    #   0. an engine WorktreeError abandon (worktree create/merge failure from ANY
    #      dispatch round) SUPERSEDES everything → return it, NO commit. On this
    #      path phase4_outcome/review_outcome may be None (the dispatch raised
    #      before assigning them), so it MUST be checked first.
    #   1. a review abandonment (roster-too-small or fix-loop exhaustion)
    #      SUPERSEDES Phase-4 success → return it, NO commit;
    #   2. Phase-4 itself abandoned (skipped review) → return it, NO commit;
    #   3. clean Phase-4 success → run the CI-mirror gate; a cycle-introduced
    #      break abandons → return that, NO commit;
    #   4. else commit the merged work, stamp the real commit_sha + Memex slug.
    if worktree_abandon is not None:
        return worktree_abandon
    if review_outcome is not None:
        return review_outcome
    # phase4_outcome is always assigned by the time we reach here on a non-
    # WorktreeError path (the try body assigns it before any review dispatch).
    if phase4_outcome is None or phase4_outcome.get("status") != "success":
        return phase4_outcome

    # Clean success → CI-mirror gate (§2). Reviewer-driven fixes are NOT re-CI'd
    # — this single post-Phase-4 gate matches team mode's documented parity.
    last_ci_results, ci_abandon = _run_ci_gate(
        clone_path,
        test_command,
        ci_baseline,
        subject=subject,
        participants=participants,
    )
    if ci_abandon is not None:
        return ci_abandon

    # Commit the merged work + read back the real SHA (§1). The minutes slug is
    # `kaizen:cycle:{run_id}-{cycle_n}` (or `kaizen:cycle:host-{cycle_n}` when no
    # run_id), mirroring team mode's Memex-slug convention. n_tests is the real
    # pytest pass count parsed from the gate's `tests` output.
    #
    # `allow_empty=True`: atelier's engine EAGER-MERGES each Phase-4 writer's
    # worktree into the clone HEAD as `--no-ff` merge commits, so the impl work
    # is ALREADY committed by the time we get here and the working tree is clean.
    # The kaizen cycle commit stamps the standard cycle message (the PR
    # title/body render from it) on top of those merges; without --allow-empty,
    # `git commit` over a clean tree would exit 1.
    minutes_ref = (
        f"kaizen:cycle:{run_id}-{cycle_n}" if run_id is not None else f"kaizen:cycle:host-{cycle_n}"
    )
    n_tests = parse_pytest_pass_count((last_ci_results or {}).get("tests", {}).get("output", ""))
    commit_sha = commit_cycle_and_sha(
        clone_dir=clone_path,
        cycle_n=cycle_n,
        decisions=["host-mode cycle"],
        participants=list(participants),
        n_tests=n_tests,
        subject=subject or "host-mode",
        minutes_rel_path=minutes_ref,
        allow_empty=True,
    )
    phase4_outcome["commit_sha"] = commit_sha
    phase4_outcome["minutes_memex_slug"] = minutes_ref
    return phase4_outcome
