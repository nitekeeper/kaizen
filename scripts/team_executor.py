"""Team agent mode cycle executor.

Coordinates a Phase 1-5c cycle via the Agent Teams API. The actual
tool invocations (TeamCreate / SendMessage / TeamDelete) are passed in
as callables — Python cannot directly call Claude Code session tools.
The orchestrating agent (running internal/cycle/SKILL.md with mode='team')
provides the wrappers from its own tool context. Tests inject mocks.

**Cycle 4 ships only the lifecycle skeleton.** Phase 1-5c orchestration
is stubbed via a single send_message to the first roster member. A
future cycle replaces the skeleton body with the real agenda/wave/
review-loop dispatch; the lifecycle (team_create → ... → team_delete
in finally) is the load-bearing contract this cycle locks down.

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

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


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
      3. ``team_id = tools.team_create(name, members=project["expert_roster"])``
      4. try:
             run the Phase 1-5c flow (SKELETON for this cycle):
               - Phase 1 (agenda): ``tools.send_message(team_id, to=pm, message=...)``
               - Phase 2 (parallel pre-analysis): send_message to each participant
               - Phase 3 (synthesis meeting): orchestrated via send_message round-trips
               - Phase 4 (implementation): each owner's send_message returns their work
               - Phase 5a-c (destructive check, tests, commit): same orchestration
         finally:
             ``tools.team_delete(team_id)``  # ALWAYS — even on abandon or exception
      5. Return the outcome dict matching the contract in the module docstring

    For THIS cycle, the Phase 1-5c implementation is a SKELETON that:
      - Calls ``team_create`` / ``send_message`` / ``team_delete`` in the right order
      - Returns a success outcome on the happy path
      - Returns an abandonment outcome with ``phase_reached='meeting'`` and
        ``reason='other'`` when ``send_message`` returns a string starting
        with ``"ABANDON:"`` (the wrapper's convention for "this participant
        signaled abandonment")

    Real Phase 1-5c semantics are out of scope for this cycle — the
    skeleton's purpose is to prove the LIFECYCLE invariants (team_create
    fires, team_delete ALWAYS fires, outcome dict shape is honored) so
    that a future cycle can fill in the agenda/meeting/wave logic without
    re-litigating the dispatch architecture.

    The ``team_delete``-in-``finally`` invariant is the critical behavioral
    contract — without it, orphan teams pollute the user's Claude Code
    session across cycles.
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
    for method_name in ("team_create", "send_message", "team_delete"):
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

    team_id = tools.team_create(team_name, members=roster if roster else [pm])

    try:
        # SKELETON: this single send_message stands in for the full Phase
        # 1-5c orchestration (agenda → pre-analysis → synthesis → waves
        # → destructive check → tests → commit). A future cycle will
        # expand this into the real flow. The lifecycle invariants
        # (team_create → send_message → team_delete in a finally) are
        # what this skeleton is here to lock down.
        response = tools.send_message(
            team_id,
            to=pm,
            message=(
                f"Kaizen cycle {cycle_n} (skeleton). "
                f"Subject: {subject or 'PM-directed'}. "
                "Respond with a one-line agenda or prefix 'ABANDON:' to abandon."
            ),
        )

        if isinstance(response, str) and response.startswith("ABANDON:"):
            reasoning = response[len("ABANDON:") :].strip()
            outcome_acc.abandoned = True
            outcome_acc.phase_reached = "meeting"
            outcome_acc.reason = "other"
            outcome_acc.detail = (
                f"Participant {pm} signaled abandonment during Phase 1 (agenda): {reasoning}"
            )
        else:
            outcome_acc.decisions.append(response if isinstance(response, str) else str(response))
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
        "commit_sha": "(skeleton)",
        "minutes_memex_slug": f"kaizen:cycle:{run_row['id']}-{cycle_n}",
        "participants": outcome_acc.participants,
    }
