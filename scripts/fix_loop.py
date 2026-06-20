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

import logging
import re
from collections import Counter
from dataclasses import asdict, dataclass, field

from scripts.abandonment import VALID_PHASES, VALID_REASONS

_log = logging.getLogger(__name__)

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


def _summarise_convergence(
    state: FixLoopState,
    peer_unconfirmed: set[str] | None = None,
    non_routable: set[str] | None = None,
) -> str:
    """Build a human-readable convergence summary from the history.

    ``peer_unconfirmed`` (M8a-2c LOW-1) is the set of blocker/major finding_ids
    that survived without any peer cross-confirm. When NON-EMPTY a neutral
    disclosure clause is appended; when None or empty NOTHING is appended (so the
    summary shape is unchanged for the common path). Disclosure is neutral — it
    does not editorialize on whether the findings are real.

    ``non_routable`` (M8b Bug#4) is the set of blocker/major finding_ids whose
    target was a directory / non-file and so could NOT be dispatched to a
    file-owner writer. When NON-EMPTY a disclosure clause naming them is appended;
    these are the findings that GUARANTEED non-convergence (no writer could ever
    fix them), so the exhaustion is explained rather than mysterious.
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
    if non_routable:
        ids = ", ".join(sorted(non_routable))
        summary += f" Non-routable (directory/non-file target, no file-owner writer): {ids}."
    return summary


def build_abandonment_outcome(
    state: FixLoopState,
    subject: str | None,
    participants: list[str],
    peer_unconfirmed: set[str] | None = None,
    non_routable: set[str] | None = None,
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
    convergence_summary = _summarise_convergence(state, peer_unconfirmed, non_routable)
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


# ── Reviewer-response parsing (relocated from team_executor, M8c-1) ──────────

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

    Finding lines are parsed FIRST: each line matching
    ``[severity] file:line — text`` becomes a `Finding`. Lines that don't
    match are silently skipped — reviewers may include prose before/after
    their finding list. The wire-protocol §4 contract is preserved: a pure
    ``NO ISSUES`` reply contains no finding lines, so it still parses to
    ``[]``. Crucially, a MIXED reply ("...no issues..., but\\n[blocker]
    ...") yields its findings — a 'no issues' substring must never
    short-circuit past explicit finding lines (P2/F9: the review loop must
    not collapse). `prefix` is used to build stable per-iteration finding
    ids (e.g. ``R1-1``, ``R1-2``).
    """
    if not response:
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


# F12 (audit cleanup): map a CI-mirror check name to the abandonment-reason
# enum value that best describes its failure category. The reasons enum
# is documented in scripts/abandonment.VALID_REASONS and migrations/005.
#
# Ordering matters for the multi-category case — see `_pick_highest_reason`.
_CHECK_TO_REASON = {
    "ruff_check": "lint_failed",
    "ruff_format": "lint_failed",
    "bandit": "security_failed",
    "pip_audit": "sca_failed",
    "tests": "tests_unrecoverable",
}

# Highest-severity first: when multiple checks fail in the same wave, we
# pick the most severe category so the abandonment is taxonomically right
# (a test break shadows a lint break, a security break shadows an SCA
# break, etc.). The order is fixed deterministically:
#   tests_unrecoverable > security_failed > sca_failed > lint_failed
_REASON_SEVERITY_ORDER = (
    "tests_unrecoverable",
    "security_failed",
    "sca_failed",
    "lint_failed",
)


def _pick_highest_reason(failed_checks: list[str]) -> str:
    """Map ``failed_checks`` → the single highest-severity abandonment reason.

    Unrecognised check names fall back to ``tests_unrecoverable`` so a
    mystery break still surfaces with a plausible category (and the detail
    string carries the raw check names so triage can see the truth).
    """
    reasons = {_CHECK_TO_REASON.get(name, "tests_unrecoverable") for name in failed_checks}
    for r in _REASON_SEVERITY_ORDER:
        if r in reasons:
            return r
    return "tests_unrecoverable"


def _diff_ci_results(
    baseline: dict[str, dict] | None,
    current: dict[str, dict],
) -> tuple[list[str], list[str]]:
    """Split current-wave failures into cycle-introduced vs. pre-existing.

    F10 (audit cleanup): a CI mirror runs at every Phase 4 wave boundary.
    Without a baseline, ANY ``status == "fail"`` aborts the cycle — which
    means a host with a pre-existing ruff lint debt (or a stale pip-audit
    CVE) causes every kaizen run to abandon, regardless of whether the
    cycle's edits introduced the breakage.

    Returns a 2-tuple ``(cycle_introduced, pre_existing)`` keyed by check
    name. A check fails as "cycle-introduced" iff its current status is
    ``fail`` AND its baseline status (when baseline is non-None) was NOT
    ``fail``. When ``baseline`` is None every fail is treated as
    cycle-introduced — there is no baseline to diff against.
    """
    cycle_introduced: list[str] = []
    pre_existing: list[str] = []
    for name, result in current.items():
        if result.get("status") != "fail":
            continue
        if baseline is not None and baseline.get(name, {}).get("status") == "fail":
            pre_existing.append(name)
        else:
            cycle_introduced.append(name)
    return cycle_introduced, pre_existing
