"""Team agent mode cycle executor.

Coordinates a Phase 1-5c cycle via the Agent Teams API. The actual
tool invocations (TeamCreate / SendMessage / TeamDelete) are passed in
as callables — Python cannot directly call Claude Code session tools.
The orchestrating agent (running internal/cycle/SKILL.md with mode='team')
provides the wrappers from its own tool context. Tests inject mocks.

# Architecture

The Agent Teams API (TeamCreate, SendMessage, TeamDelete) is a Claude Code
SESSION-SCOPED API. Python cannot directly invoke those tools — they only
exist in an active Claude Code agent context. A naive
``subprocess.run(["claude", "TeamCreate", ...])`` is not how it works.

The honest answer: this executor cannot directly call the tools, but it
CAN be a coordinator that accepts injected tool wrappers. The
orchestrating Claude Code agent (running ``internal/cycle/SKILL.md`` with
``mode='team'``) provides the wrappers from its own tool context. Tests
inject mocks.

# Availability guard

``CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`` must be set in the environment.
If it is absent, ``team_cycle_executor`` raises ``TeamToolsUnavailableError``
immediately so the caller can surface a clear error rather than silently
falling back to subagent mode.

# Wire protocol — agent response messages

The wrapper-side agent responses come back as **plain strings**. To keep
the orchestration deterministic and testable, this executor reads the
strings using a small set of conventions defined here as the single
source of truth (the production wrapper must follow them — drift = bugs):

1. **ABANDON signal** — any response whose first non-whitespace text is
   the prefix ``ABANDON:`` means "this participant cannot continue";
   the rest of the line is treated as the reason text.

2. **Agenda items (Phase 1)** — the PM's response is a list of items,
   one per non-blank line. The whole-line content is the agenda item.
   Lines starting with ``#`` are treated as headers and ignored.

3. **Action Items DAG (Phase 3 close)** — the PM's close response is
   expected to contain a fenced ```json``` block whose body parses to a
   list of Action Item dicts matching ``scripts.dag.validate_dag``'s
   schema (id/touches/reads/depends_on/wave/owner). If no JSON block is
   found, the parser returns ``[]`` and the executor surfaces this as a
   no_consensus abandonment with a clear detail message.

4. **Reviewer findings (Phase 5b')** — each reviewer's response is a
   list of finding lines, one per line, in the format
   ``[severity] file:line — text``. A response containing the literal
   substring ``NO ISSUES`` (case-insensitive) returns an empty list,
   which the fix loop treats as "this reviewer is satisfied".

These conventions are minimal-by-design: the orchestrator doesn't try
to parse free-form prose; the wrapper-side agents emit the small
amount of structure documented above. If a future cycle wants a richer
protocol, it should evolve this module and ``internal/cycle/SKILL.md``
together.

# Outcome dict (matches internal/cycle/SKILL.md)

    # Success
    {
        "status": "success",
        "subject": str | None,
        "commit_sha": str,
        "minutes_memex_slug": str,
        "participants": list[str],
    }

    # Abandoned
    {
        "status": "abandoned",
        "subject": str | None,
        "participants": list[str],
        "phase_reached": "agenda" | "meeting" | "implementation" | "test" | "review" | "push",
        "reason": "no_consensus" | "destructive_rejected" | "tests_unrecoverable" | "review_unrecoverable" | "other",
        "detail": str,
        "artifacts": list[str],
        # Optional Phase 5b' review-loop fields — present only when
        # reason='review_unrecoverable'. See scripts/abandonment.py.
        "review_iteration_count": int | None,
        "unresolved_findings": list[dict] | None,
        "convergence_summary": str | None,
        "reviewer_attribution": dict | None,
    }
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from scripts.ci_runner import run_ci_checks
from scripts.cycle_git import commit_cycle
from scripts.dag import validate_dag
from scripts.dispatch_templates import (
    phase_1_agenda,
    phase_2_preanalysis,
    phase_3_close,
    phase_3_debate,
    phase_3_open,
    phase_4_implementer,
    phase_5b_ci_failure,
    phase_5b_prime_fix,
    phase_5b_prime_pm_acceptance,
    phase_5b_prime_reviewer,
)
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

_log = logging.getLogger(__name__)


class TeamToolsUnavailableError(RuntimeError):
    """Raised when the Agent Teams API is not available in the current session.

    Set CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 in the environment and ensure
    TeamCreate / SendMessage / TeamDelete tools are enabled before using
    team agent mode. Production callers MUST also inject a `TeamTools`
    implementation — Python cannot directly call Claude Code session tools.
    """


class TeamTools(Protocol):
    """The injected tool surface team_cycle_executor needs.

    Production callers (the orchestrating agent) provide wrappers that
    invoke the real Claude Code session tools. Tests inject mocks.

    The wrapper is responsible for any awaiting / response unwrapping —
    from the executor's point of view every method is synchronous.
    """

    def team_create(self, name: str, members: list[str]) -> str:
        """Create a named team with the given member role ids. Returns team_id."""
        ...

    def send_message(self, team_id: str, to: str, message: str) -> str:
        """Send a message; returns the recipient's response synchronously."""
        ...

    def send_message_many(self, messages: list[dict]) -> list[str]:
        """Batch dispatch — enqueue N messages in parallel; return their
        responses in input order. Each dict has ``team_id``, ``to``,
        ``message``. Used by Phase 2 fan-out, Phase 3 Star-open broadcast,
        and Phase 5b' parallel reviewer dispatch — see
        docs/kaizen/2026-05-24-bridge-smoke-2.md GAP-4 for the motivation
        (sequential send_message is the wall-clock bottleneck of a cycle).
        """
        ...

    def team_delete(self, team_id: str) -> None:
        """Tear down the team."""
        ...


@dataclass
class TeamCycleOutcome:
    """Internal accumulator used by the executor; converted to outcome dict at exit."""

    participants: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    abandoned: bool = False
    phase_reached: str | None = None
    reason: str | None = None
    detail: str = ""
    artifacts: list[str] = field(default_factory=list)
    review_iteration_count: int | None = None
    unresolved_findings: list[dict] | None = None
    convergence_summary: str | None = None
    reviewer_attribution: dict | None = None


def _check_team_tools_available() -> None:
    """Raise TeamToolsUnavailableError if the Agent Teams env var is absent.

    This is a fast pre-flight guard — it does not attempt to call the tools,
    only checks for the environment variable that enables them.  A missing
    var means the tools will not be surfaced in the session, so trying to
    invoke them would produce an obscure ToolNotFound error deep in the cycle.
    Failing loudly here produces a human-readable message instead.
    """
    flag = os.environ.get("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", "")
    if flag.strip() not in ("1", "true", "True", "TRUE", "yes", "YES"):
        raise TeamToolsUnavailableError(
            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS is not set (or not truthy). "
            "Set it to '1' to enable the Agent Teams API tools "
            "(TeamCreate, SendMessage, TeamDelete) before running kaizen in "
            "team agent mode.\n"
            "  export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1\n"
            "Or switch to subagent mode: kaizen:improve <url> --mode subagent"
        )


# ── Wire protocol helpers ─────────────────────────────────────────────────


def _is_abandon(response: str) -> bool:
    """True if `response` is an ABANDON signal per the wire protocol."""
    return isinstance(response, str) and response.lstrip().startswith("ABANDON:")


def _abandon_reason(response: str) -> str:
    """Extract the reason text after the ABANDON: prefix; returns '' if none."""
    stripped = response.lstrip()
    if not stripped.startswith("ABANDON:"):
        return ""
    return stripped[len("ABANDON:") :].strip()


def _parse_agenda_items(response: str) -> list[str]:
    """Parse the PM's Phase-1 agenda response into items, one per non-blank line.

    Lines starting with ``#`` are skipped (treated as section headers).
    Empty input → empty list. Whitespace is stripped per item.

    See module docstring §2 for the protocol.
    """
    items: list[str] = []
    for line in (response or "").splitlines():
        text = line.strip()
        if not text:
            continue
        if text.startswith("#"):
            continue
        items.append(text)
    return items


_JSON_BLOCK_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)


def _parse_action_items(response: str) -> list[dict]:
    """Parse the PM's Phase-3 close response into Action Item dicts.

    Looks for the first ```json``` fenced block and parses its body as a
    JSON list. Each element is returned as-is — `scripts.dag.validate_dag`
    will fail-loud on shape mismatch. Returns ``[]`` when no JSON block
    is found or when the body fails to parse (the caller surfaces this
    as a no_consensus abandonment).

    See module docstring §3 for the protocol.
    """
    if not response:
        return []
    match = _JSON_BLOCK_RE.search(response)
    if match is None:
        return []
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return data


# Accept ASCII hyphen, em-dash (U+2014), and en-dash (U+2013) as the optional
# separator between "file:line" and the finding text - real reviewers use any.
# Unicode escapes here so ruff's RUF001 ambiguous-char check stays quiet.
_FINDING_LINE_RE = re.compile(
    "^\\s*\\[(?P<severity>blocker|major|minor|nit)\\]\\s+"
    "(?P<file_line>\\S+)\\s+"
    "(?:[-\u2014\u2013]\\s*)?(?P<text>.+?)\\s*$"
)


def _parse_reviewer_response(
    response: str,
    reviewer: str,
    prefix: str,
) -> list[Finding]:
    """Parse a reviewer's response into `Finding` objects.

    Per the wire protocol (§4): a literal ``NO ISSUES`` substring (case
    insensitive) → empty list. Otherwise each line matching
    ``[severity] file:line — text`` becomes a `Finding`. Lines that don't
    match are silently skipped — reviewers may include prose before/after
    their finding list. `prefix` is used to build stable per-iteration
    finding ids (e.g. ``R1-1``, ``R1-2``).
    """
    if not response:
        return []
    if "no issues" in response.lower():
        return []
    findings: list[Finding] = []
    seq = 0
    for line in response.splitlines():
        m = _FINDING_LINE_RE.match(line)
        if m is None:
            continue
        seq += 1
        findings.append(
            Finding(
                finding_id=f"{prefix}-{seq}",
                reviewer=reviewer,
                severity=m.group("severity"),
                finding=m.group("text"),
                file_line=m.group("file_line"),
            )
        )
    return findings


def _collect_existing_files(clone_dir: Path) -> frozenset[str]:
    """Return the set of repo-relative file paths currently on disk in clone_dir.

    Used by `validate_dag` gate 3 (reads satisfiable). Walks the working
    tree, skipping the usual transient/VCS directories. Errors (e.g. clone
    doesn't exist yet) are tolerated by returning an empty frozenset — the
    DAG validator will then surface unsatisfiable-reads errors with
    meaningful messages.
    """
    if not clone_dir or not Path(clone_dir).exists():
        return frozenset()
    skip = {".git", ".ai", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"}
    out: set[str] = set()
    root = Path(clone_dir)
    try:
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            # Skip if any path part is in the skip set
            if any(part in skip for part in p.relative_to(root).parts):
                continue
            out.add(str(p.relative_to(root)))
    except OSError:
        return frozenset()
    return frozenset(out)


# ── Phase brief builders (extracted to scripts/dispatch_templates.py) ─────
#
# The 10 Phase 1-5c dispatch templates live in `scripts/dispatch_templates`
# (imported at the top of this module). Each template is a pure function
# with explicit required-kwarg validation — see that module for the wire-
# protocol-aligned bodies. The executor builds messages by calling those
# templates; no brief text is constructed here.


def _find_owner_for_finding(
    finding: Finding,
    file_to_owner: dict[str, str],
    pm: str,
) -> str:
    """Map a `Finding` to the implementer who should fix it.

    Per internal/cycle/SKILL.md Phase 5b' the IMPLEMENTER (the Action Item
    owner from Phase 3) fixes findings — never the reviewer who flagged
    them. We extract the file from `finding.file_line` ("file:line") and
    look it up in the file→owner index built at Phase 4 dispatch time.
    When the file isn't owned by any Action Item (e.g. a reviewer surfaced
    a cross-cutting issue) we fall back to the PM, who can re-route.
    """
    raw = finding.file_line or ""
    file = raw.split(":", 1)[0] if ":" in raw else raw
    owner = file_to_owner.get(file)
    if owner is None:
        _log.warning(
            "Phase 5b' finding from reviewer %s on unowned file %s — routing fix to PM %s",
            finding.reviewer,
            file,
            pm,
        )
        return pm
    return owner


# ── Outcome helpers ────────────────────────────────────────────────────────


def _abandon(
    outcome_acc: TeamCycleOutcome,
    *,
    phase_reached: str,
    reason: str,
    detail: str,
) -> TeamCycleOutcome:
    """Mark `outcome_acc` abandoned and return it. Caller returns out of try:."""
    outcome_acc.abandoned = True
    outcome_acc.phase_reached = phase_reached
    outcome_acc.reason = reason
    outcome_acc.detail = detail
    return outcome_acc


# ── Main entry point ───────────────────────────────────────────────────────


def team_cycle_executor(
    clone_dir: Path,
    project: dict,
    run_row: dict,
    cycle_n: int,
    *,
    tools: TeamTools | None = None,
) -> dict:
    """Drive a kaizen cycle via the Agent Teams API.

    ``tools`` is the injected tool wrapper. When ``None``, the executor
    raises ``TeamToolsUnavailableError`` — production callers MUST inject
    real wrappers (Python cannot directly call Claude Code session tools);
    tests MUST inject mocks.

    Lifecycle:

      1. ``_check_team_tools_available()`` — env-var preflight
      2. If ``tools is None``: raise ``TeamToolsUnavailableError`` with a
         message telling the caller to inject a ``TeamTools`` implementation
      3. Runtime Protocol shape check — every required method callable
      4. ``team_id = tools.team_create(name, members=project["expert_roster"])``
      5. try:
             - Phase 1 (agenda): PM proposes agenda items
             - Phase 2 (pre-analysis): each non-PM participant proposes
             - Phase 3 (synthesis): Star → Mesh → Star, PM consolidates Action
               Items DAG, validate_dag enforces the 4 gates
             - Phase 4 (waves): each wave dispatches its owners' work, then
               runs CI at the wave boundary
             - Phase 5b' (reviewers): disjoint reviewer selection + fix loop
               (max 5 iterations, blocker+major findings drive re-review)
             - Phase 5c (commit): real git commit via commit_cycle()
         finally:
             ``tools.team_delete(team_id)``  # ALWAYS — even on abandon or exception
      6. Return the outcome dict matching the contract in the module docstring

    The ``team_delete``-in-``finally`` invariant is the critical behavioral
    contract — without it, orphan teams pollute the user's Claude Code
    session across cycles.

    # NOTE Phase 2 is sequential under the executor's view: `tools.send_message`
    # is synchronous from this side per the wrapper contract (the wrapper is
    # responsible for the underlying parallelism, if any). The real Agent
    # Teams API could fan-out, but the wrapper's contract is one-call →
    # one-response.
    """
    _check_team_tools_available()

    if tools is None:
        raise TeamToolsUnavailableError(
            "team_cycle_executor was called with tools=None. "
            "Python cannot directly call Claude Code session tools "
            "(TeamCreate / SendMessage / TeamDelete) — the orchestrating "
            "agent (running internal/cycle/SKILL.md with mode='team') MUST "
            "inject a TeamTools implementation that wraps its own tool "
            "context. Tests MUST inject a mock TeamTools."
        )

    # Runtime shape check — `TeamTools` is a typing.Protocol (static-only),
    # so passing e.g. object() would blow up mid-cycle (AFTER team_create)
    # with an opaque AttributeError. Failing here guarantees we never
    # leave a half-formed team behind because the injection was malformed.
    for method_name in ("team_create", "send_message", "send_message_many", "team_delete"):
        if not callable(getattr(tools, method_name, None)):
            raise TeamToolsUnavailableError(
                f"tools is missing required method: {method_name!r}. "
                "Inject a TeamTools-compatible wrapper (see Protocol definition)."
            )

    # Import here so a fresh import of this module under a patched
    # scripts.abandonment in tests still picks up the patched frozensets.
    from scripts.abandonment import VALID_PHASES, VALID_REASONS

    subject = run_row.get("subject")
    roster: list[str] = list(project.get("expert_roster") or [])
    # Pick a deterministic first participant — PM if present, else first
    # roster member, else a sane default.
    pm = roster[0] if roster else "pm-1"

    team_name = f"kaizen-cycle-{run_row.get('id', 0)}-{cycle_n}"

    outcome_acc = TeamCycleOutcome(participants=list(roster) if roster else [pm])

    commit_sha: str | None = None
    team_id = tools.team_create(team_name, members=roster if roster else [pm])

    try:
        # ── Phase 1 — Agenda (PM) ─────────────────────────────────────────
        agenda_response = tools.send_message(
            team_id,
            to=pm,
            message=phase_1_agenda(subject=subject, cycle_n=cycle_n),
        )
        if _is_abandon(agenda_response):
            _abandon(
                outcome_acc,
                phase_reached="agenda",
                reason="other",
                detail=f"PM {pm} abandoned at agenda: {_abandon_reason(agenda_response)}",
            )
        else:
            agenda_items = _parse_agenda_items(agenda_response)
            if not agenda_items:
                _abandon(
                    outcome_acc,
                    phase_reached="agenda",
                    reason="other",
                    detail="PM produced no agenda items (response empty or unparseable).",
                )

        # ── Phase 2 — Parallel pre-analysis ───────────────────────────────
        # GAP-4 (docs/kaizen/2026-05-24-bridge-smoke-2.md): batch-dispatch
        # via send_message_many so the N pre-analysis briefs go out in one
        # transaction and the S1 wrapper services them in parallel rather
        # than serially round-tripping each. Order is preserved (input
        # roster order == output response order).
        proposals: list[dict] = []
        if not outcome_acc.abandoned:
            phase_2_recipients = [p for p in roster if p != pm]
            if phase_2_recipients:
                phase_2_messages = [
                    {
                        "team_id": team_id,
                        "to": participant,
                        "message": phase_2_preanalysis(
                            agenda_items=agenda_items, participant=participant
                        ),
                    }
                    for participant in phase_2_recipients
                ]
                phase_2_responses = tools.send_message_many(phase_2_messages)
                for participant, resp in zip(phase_2_recipients, phase_2_responses, strict=True):
                    if _is_abandon(resp):
                        _abandon(
                            outcome_acc,
                            phase_reached="meeting",
                            reason="other",
                            detail=(
                                f"{participant} abandoned at pre-analysis: {_abandon_reason(resp)}"
                            ),
                        )
                        break
                    proposals.append({"agent": participant, "raw": resp or ""})

        # ── Phase 3 — Synthesis meeting (Star → Mesh → Star) ──────────────
        action_items: list[dict] = []
        waves: tuple[tuple[str, ...], ...] = ()
        if not outcome_acc.abandoned:
            # Star open: brief every roster member with the proposals
            # GAP-4: batch-dispatch — one transaction, N parallel briefs.
            if roster:
                tools.send_message_many(
                    [
                        {
                            "team_id": team_id,
                            "to": participant,
                            "message": phase_3_open(proposals=proposals),
                        }
                        for participant in roster
                    ]
                )

            # Mesh (simplified): each participant signals consensus.
            # GAP-4: batch-dispatch as well — same all-to-all fan-out shape
            # as Star-open; serialising it was the same Phase 2 bottleneck.
            agreements: list[dict] = []
            if roster:
                debate_responses = tools.send_message_many(
                    [
                        {
                            "team_id": team_id,
                            "to": participant,
                            "message": phase_3_debate(),
                        }
                        for participant in roster
                    ]
                )
                for participant, resp in zip(roster, debate_responses, strict=True):
                    if _is_abandon(resp):
                        _abandon(
                            outcome_acc,
                            phase_reached="meeting",
                            reason="no_consensus",
                            detail=(
                                f"{participant} could not reach consensus: {_abandon_reason(resp)}"
                            ),
                        )
                        break
                    agreements.append({"agent": participant, "raw": resp or ""})

        if not outcome_acc.abandoned:
            # Star close: PM consolidates into Action Items DAG
            close_resp = tools.send_message(
                team_id,
                to=pm,
                message=phase_3_close(proposals=proposals, agreements=agreements),
            )
            if _is_abandon(close_resp):
                _abandon(
                    outcome_acc,
                    phase_reached="meeting",
                    reason="no_consensus",
                    detail=f"PM could not consolidate: {_abandon_reason(close_resp)}",
                )
            else:
                action_items = _parse_action_items(close_resp)
                if not action_items:
                    _abandon(
                        outcome_acc,
                        phase_reached="meeting",
                        reason="no_consensus",
                        detail=(
                            "PM close response contained no parseable Action "
                            "Items JSON block (expected a ```json``` fenced list)."
                        ),
                    )
                else:
                    existing_files = _collect_existing_files(clone_dir)
                    try:
                        validation = validate_dag(action_items, existing_files=existing_files)
                    except ValueError as shape_err:
                        _abandon(
                            outcome_acc,
                            phase_reached="meeting",
                            reason="no_consensus",
                            detail=(f"Action Items DAG shape error: {shape_err}"),
                        )
                    else:
                        if not validation.ok:
                            _abandon(
                                outcome_acc,
                                phase_reached="meeting",
                                reason="no_consensus",
                                detail=(
                                    "Action Items DAG failed validation: "
                                    + "; ".join(str(e) for e in validation.errors)
                                ),
                            )
                        else:
                            waves = validation.waves

        # ── Phase 4 — Wave-based dispatch ─────────────────────────────────
        # Build file→owner index up-front so Phase 5b' fix routing
        # (implementer fixes, NOT reviewer) has a deterministic lookup
        # table. Per internal/cycle/SKILL.md: "Implementers (Owner from
        # Phase 3 carries forward) fix all blocker + major issues."
        file_to_owner: dict[str, str] = {}
        if action_items:
            for item in action_items:
                owner = item.get("owner") or pm
                for f in item.get("touches", []):
                    file_to_owner.setdefault(f, owner)
        if not outcome_acc.abandoned and waves:
            items_by_id = {item["id"]: item for item in action_items}
            for wave_n, wave_ids in enumerate(waves, start=1):
                wave_abandoned = False
                for ai_id in wave_ids:
                    item = items_by_id[ai_id]
                    owner = item.get("owner") or pm
                    impl_resp = tools.send_message(
                        team_id,
                        to=owner,
                        message=phase_4_implementer(item=item, wave_n=wave_n),
                    )
                    if _is_abandon(impl_resp):
                        _abandon(
                            outcome_acc,
                            phase_reached="implementation",
                            reason="other",
                            detail=(
                                f"Owner {owner} abandoned AI {ai_id}: {_abandon_reason(impl_resp)}"
                            ),
                        )
                        wave_abandoned = True
                        break
                    outcome_acc.decisions.append(f"AI {ai_id}: {(impl_resp or '')[:200]}")
                if wave_abandoned:
                    break
                # CI mirror at every wave boundary
                test_command = project.get("test_command") or "pytest"
                all_passed, results = run_ci_checks(clone_dir, test_command)
                if not all_passed:
                    failed = [name for name, (ok, _) in results.items() if not ok]
                    _abandon(
                        outcome_acc,
                        phase_reached="test",
                        reason="tests_unrecoverable",
                        detail=phase_5b_ci_failure(
                            wave_n=wave_n,
                            failed_checks=failed,
                        ),
                    )
                    break

        # ── Phase 5b' — Independent reviewers + fix loop ──────────────────
        if not outcome_acc.abandoned and action_items:
            implementers = [item.get("owner") for item in action_items if item.get("owner")]
            disjoint_pool_size = len([r for r in roster if r not in set(implementers)])
            n_reviewers = min(3, disjoint_pool_size) if disjoint_pool_size > 0 else 0
            if n_reviewers < 1:
                _abandon(
                    outcome_acc,
                    phase_reached="review",
                    reason="other",
                    detail=(
                        "Cannot select any disjoint reviewer — roster too small "
                        f"(roster={len(roster)}, implementers={len(set(implementers))})."
                    ),
                )
            else:
                try:
                    reviewers = select_reviewers(
                        roster,
                        implementers,
                        n=n_reviewers,
                        preferred_lenses=["security", "architect", "prompt", "safety"],
                    )
                except InsufficientRosterError as e:
                    _abandon(
                        outcome_acc,
                        phase_reached="review",
                        reason="other",
                        detail=f"Cannot select disjoint reviewers: {e}",
                    )
                else:
                    state = FixLoopState()
                    review_outcome: dict | None = None
                    # FixLoopExhausted is unreachable here because
                    # should_continue returns False at iteration==MAX before
                    # start_iteration is called. See scripts/fix_loop.py
                    # docstring for the contract.
                    while True:
                        iter_n = start_iteration(state)
                        # Carry forward the previous round's findings so
                        # iteration 2+ reviewers do incremental review
                        # rather than a fresh scan (Major 3).
                        prior = state.history[-1] if state.history else None
                        findings: list[Finding] = []
                        # GAP-4: batch-dispatch reviewer briefs in parallel
                        # (was sequential; with 3 reviewers and ~60s/reply
                        # this was ~3x the necessary wall-clock per round).
                        reviewer_messages = [
                            {
                                "team_id": team_id,
                                "to": reviewer,
                                "message": phase_5b_prime_reviewer(
                                    iter_n=iter_n,
                                    action_items=action_items,
                                    prior_findings=prior,
                                ),
                            }
                            for reviewer in reviewers
                        ]
                        reviewer_responses = tools.send_message_many(reviewer_messages)
                        for reviewer, resp in zip(reviewers, reviewer_responses, strict=True):
                            findings.extend(
                                _parse_reviewer_response(resp or "", reviewer, prefix=f"R{iter_n}")
                            )
                        record_findings(state, findings)
                        latest_blockers = [
                            f for f in findings if f.severity in _BLOCKING_SEVERITIES
                        ]
                        if not latest_blockers:
                            break  # zero blocking findings → clean exit
                        # Ask the PM whether the remaining findings are
                        # acceptable for this cycle. PM-acceptance is a
                        # legit exit per SKILL contract (Major 2).
                        pm_resp = tools.send_message(
                            team_id,
                            to=pm,
                            message=phase_5b_prime_pm_acceptance(
                                findings=latest_blockers, iter_n=iter_n
                            ),
                        )
                        pm_accepts = (pm_resp or "").strip().upper().startswith("ACCEPT")
                        if not should_continue(state, pm_accepts_remaining=pm_accepts):
                            if pm_accepts:
                                # Clean exit — PM ruled remaining issues
                                # acceptable; no abandonment outcome.
                                break
                            # MAX_ITERATIONS reached with blockers still
                            # present — this is the review_unrecoverable case.
                            review_outcome = build_abandonment_outcome(
                                state,
                                subject=subject,
                                participants=outcome_acc.participants,
                            )
                            break
                        # Fix round — dispatch fixes for blocker+major findings.
                        # Per SKILL: the IMPLEMENTER (Action Item owner) fixes,
                        # never the reviewer who flagged it (Major 1).
                        fix_loop_aborted = False
                        for finding in latest_blockers:
                            fix_owner = _find_owner_for_finding(finding, file_to_owner, pm)
                            fix_resp = tools.send_message(
                                team_id,
                                to=fix_owner,
                                message=phase_5b_prime_fix(finding=finding),
                            )
                            if _is_abandon(fix_resp):
                                review_outcome = build_abandonment_outcome(
                                    state,
                                    subject=subject,
                                    participants=outcome_acc.participants,
                                )
                                fix_loop_aborted = True
                                break
                        if fix_loop_aborted:
                            break

                    if review_outcome is not None:
                        # Bubble the fix_loop-built abandonment fields up so
                        # the success path is bypassed and the dict shape is
                        # the canonical Phase 5b' review_unrecoverable form.
                        _abandon(
                            outcome_acc,
                            phase_reached=review_outcome["phase_reached"],
                            reason=review_outcome["reason"],
                            detail=review_outcome["detail"],
                        )
                        outcome_acc.review_iteration_count = review_outcome[
                            "review_iteration_count"
                        ]
                        outcome_acc.unresolved_findings = review_outcome["unresolved_findings"]
                        outcome_acc.convergence_summary = review_outcome["convergence_summary"]
                        outcome_acc.reviewer_attribution = review_outcome["reviewer_attribution"]

        # ── Phase 5c — Commit ────────────────────────────────────────────
        if not outcome_acc.abandoned:
            minutes_rel = (
                f"docs/kaizen/{datetime.date.today().isoformat()}-cycle-{cycle_n}-minutes.md"
            )
            commit_cycle(
                clone_dir=clone_dir,
                cycle_n=cycle_n,
                decisions=outcome_acc.decisions or ["team-mode cycle"],
                participants=outcome_acc.participants,
                n_tests=0,
                subject=subject or "team-mode",
                minutes_rel_path=minutes_rel,
            )
            rev = subprocess.run(
                ["git", "-C", str(clone_dir), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            )
            commit_sha = rev.stdout.strip()
    finally:
        # CRITICAL INVARIANT: team_delete ALWAYS fires — even on exception
        # or abandonment — so the user's Claude Code session does not leak
        # named teams across cycles.
        tools.team_delete(team_id)

    if outcome_acc.abandoned:
        assert outcome_acc.phase_reached in VALID_PHASES, (
            f"BUG: phase_reached={outcome_acc.phase_reached!r} not in VALID_PHASES"
        )
        assert outcome_acc.reason in VALID_REASONS, (
            f"BUG: reason={outcome_acc.reason!r} not in VALID_REASONS"
        )
        return {
            "status": "abandoned",
            "subject": subject,
            "participants": outcome_acc.participants,
            "phase_reached": outcome_acc.phase_reached,
            "reason": outcome_acc.reason,
            "detail": outcome_acc.detail,
            "artifacts": outcome_acc.artifacts,
            "review_iteration_count": outcome_acc.review_iteration_count,
            "unresolved_findings": outcome_acc.unresolved_findings,
            "convergence_summary": outcome_acc.convergence_summary,
            "reviewer_attribution": outcome_acc.reviewer_attribution,
        }

    return {
        "status": "success",
        "subject": subject,
        "commit_sha": commit_sha or "",
        "minutes_memex_slug": f"kaizen:cycle:{run_row['id']}-{cycle_n}",
        "participants": outcome_acc.participants,
    }
