"""Phase 5b' fix-loop iteration tracker.

Per internal/cycle/SKILL.md Phase 5b', the review fix loop runs at most
5 iterations. Each iteration: implementer fixes blocker+major findings,
reviewers re-examine the diff, produce a new consolidated report. Loop
exits when (a) the new report has zero unresolved issues, OR (b) PM
rules remaining issues acceptable, OR (c) MAX_ITERATIONS is reached
(triggers `review_unrecoverable` abandonment).

This module is the single source of truth for the iteration counter
and the abandonment-shape construction. Pure functions + a small
dataclass; no DB, no I/O.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field

from scripts.abandonment import VALID_PHASES, VALID_REASONS

MAX_ITERATIONS: int = 5

# Severity classification — blocker and major are blocking; minor and nit are not.
_BLOCKING_SEVERITIES: frozenset[str] = frozenset({"blocker", "major"})
# Full severity menu — used for fail-loud validation on Finding construction.
# A reviewer typo like "blocer" would otherwise silently classify as non-blocking
# and exit the fix loop early, hiding a real blocker.
_VALID_SEVERITIES: frozenset[str] = frozenset({"blocker", "major", "minor", "nit"})


@dataclass(frozen=True)
class Finding:
    """One reviewer-flagged issue surviving an iteration."""

    finding_id: str  # stable identifier across iterations (e.g. "F-1", "F-2")
    reviewer: str  # role id (e.g. "security-engineer-1")
    severity: str  # one of: blocker | major | minor | nit
    finding: str  # what's wrong, file:line included
    file_line: str  # "file:line" — denormalised for the renderer

    def __post_init__(self) -> None:
        if self.severity not in _VALID_SEVERITIES:
            raise ValueError(
                f"severity={self.severity!r} not in {sorted(_VALID_SEVERITIES)} — "
                f"a reviewer typo would silently exit the fix loop; rejecting fail-loud"
            )


@dataclass
class FixLoopState:
    """Mutable state of the fix loop."""

    iteration: int = 0  # 0 = before first review; increments at the START of each iteration
    history: list[list[Finding]] = field(default_factory=list)
    # `history[i]` = the unresolved-findings list at the END of iteration i+1


class FixLoopExhausted(RuntimeError):
    """Raised when MAX_ITERATIONS is reached with unresolved blocker+major findings."""


def start_iteration(state: FixLoopState) -> int:
    """Bump the counter and return the new iteration number (1..MAX_ITERATIONS).

    Raises FixLoopExhausted if called when state.iteration >= MAX_ITERATIONS.
    """
    if state.iteration >= MAX_ITERATIONS:
        raise FixLoopExhausted(
            f"fix loop exhausted: already ran {state.iteration} iteration(s) "
            f"(MAX_ITERATIONS={MAX_ITERATIONS})"
        )
    state.iteration += 1
    return state.iteration


def record_findings(state: FixLoopState, findings: list[Finding]) -> None:
    """Append the END-of-iteration findings to the history.

    Must be called once per iteration after the reviewer meeting concludes.
    """
    state.history.append(list(findings))


def should_continue(state: FixLoopState, pm_accepts_remaining: bool = False) -> bool:
    """Decide whether to run another iteration.

    Returns False (exit loop) when ANY of:
      - the latest findings list has zero blocker AND zero major (only minor/nit)
      - `pm_accepts_remaining` is True (PM ruled remaining issues acceptable)
      - state.iteration >= MAX_ITERATIONS (exhausted)
    Returns True otherwise.

    Severity priority: blocker = major = blocking; minor and nit are non-blocking.
    """
    if pm_accepts_remaining:
        return False
    if state.iteration >= MAX_ITERATIONS:
        return False
    if not state.history:
        # No round has completed yet — nothing to decide on; keep going.
        return True
    latest = state.history[-1]
    blocking = [f for f in latest if f.severity in _BLOCKING_SEVERITIES]
    return bool(blocking)


def _summarise_convergence(state: FixLoopState, peer_unconfirmed: set[str] | None = None) -> str:
    """Build a human-readable convergence summary from the history.

    ``peer_unconfirmed`` (M8a-2c LOW-1) is the set of blocker/major finding_ids
    that survived without any peer cross-confirm. When NON-EMPTY a neutral
    disclosure clause is appended; when None or empty NOTHING is appended (so the
    summary shape is unchanged for the common path). Disclosure is neutral — it
    does not editorialize on whether the findings are real.
    """
    iterations = state.iteration
    latest = state.history[-1] if state.history else []
    n_remaining = len(latest)
    n_blocker = sum(1 for f in latest if f.severity == "blocker")
    n_major = sum(1 for f in latest if f.severity == "major")

    # Persistent finding-ids — those that appear in >1 round.
    id_counts: Counter[str] = Counter()
    for round_findings in state.history:
        seen_this_round: set[str] = set()
        for f in round_findings:
            if f.finding_id in seen_this_round:
                continue
            seen_this_round.add(f.finding_id)
            id_counts[f.finding_id] += 1
    persistent_ids = sorted(fid for fid, count in id_counts.items() if count > 1)
    persistent_str = ", ".join(persistent_ids) if persistent_ids else "none"

    summary = (
        f"After {iterations} iterations: {n_remaining} unresolved finding(s) "
        f"({n_blocker} blocker, {n_major} major). "
        f"Persistent across rounds: {persistent_str}."
    )
    if peer_unconfirmed:
        ids = ", ".join(sorted(peer_unconfirmed))
        summary += f" Not peer-confirmed (single-reviewer): {ids}."
    return summary


def build_abandonment_outcome(
    state: FixLoopState,
    subject: str | None,
    participants: list[str],
    peer_unconfirmed: set[str] | None = None,
) -> dict:
    """Construct the cycle's abandonment-outcome dict for the orchestrator.

    Returns the shape `scripts/run.py::orchestrate_run` expects when
    invoking `process_abandonment` for a `review_unrecoverable` case:

      {
        "status": "abandoned",
        "subject": <subject>,
        "participants": <participants>,
        "phase_reached": "review",        # per migration 004
        "reason": "review_unrecoverable",  # per migration 003
        "detail": <human summary>,
        "artifacts": [],
        "review_iteration_count": <state.iteration>,
        "unresolved_findings": [<dict>, ...],   # JSON-serialisable
        "convergence_summary": <text>,
        "reviewer_attribution": {<finding_id>: <reviewer>},
      }

    The unresolved_findings list contains the latest history entry as
    dicts (Finding._asdict()-style — finding_id/reviewer/severity/finding/file_line).
    The convergence_summary is auto-generated from history: which findings
    survived multiple rounds, which reviewers flagged them, etc.

    `reviewer_attribution` is a `{finding_id: reviewer_role_id}` map built
    from the LATEST round's findings. If the latest round contains two
    `Finding` instances with the same `finding_id` but different reviewers,
    the LAST one in the list wins (Python dict semantics). Callers should
    deduplicate by `finding_id` before recording findings if this matters
    for their use case.
    """
    phase_reached = "review"
    reason = "review_unrecoverable"
    # Fail-loud cross-module invariant: the literals MUST be schema-valid per
    # migration 003/004. If a future migration narrows the enums and forgets
    # to update scripts/abandonment.VALID_*, these checks blow up here
    # rather than silently inserting a row the DB CHECK will reject.
    # NOTE: explicit RuntimeError rather than `assert` — Python `-O` strips
    # asserts, and this is a production invariant that must always hold.
    if phase_reached not in VALID_PHASES:
        raise RuntimeError(
            f"fix_loop emits phase_reached={phase_reached!r} but cycle 1's allowlist guard "
            f"(scripts/run.py) requires one of {sorted(VALID_PHASES)} — schema drift detected"
        )
    if reason not in VALID_REASONS:
        raise RuntimeError(
            f"fix_loop emits reason={reason!r} but cycle 1's allowlist guard "
            f"(scripts/run.py) requires one of {sorted(VALID_REASONS)} — schema drift detected"
        )

    latest = state.history[-1] if state.history else []
    unresolved_findings = [asdict(f) for f in latest]
    reviewer_attribution = {f.finding_id: f.reviewer for f in latest}
    convergence_summary = _summarise_convergence(state, peer_unconfirmed)
    detail = (
        f"Phase 5b' fix loop exhausted after {state.iteration} iteration(s) "
        f"with {len(latest)} unresolved finding(s). {convergence_summary}"
    )

    return {
        "status": "abandoned",
        "subject": subject,
        "participants": list(participants),
        "phase_reached": phase_reached,
        "reason": reason,
        "detail": detail,
        "artifacts": [],
        "review_iteration_count": state.iteration,
        "unresolved_findings": unresolved_findings,
        "convergence_summary": convergence_summary,
        "reviewer_attribution": reviewer_attribution,
    }
