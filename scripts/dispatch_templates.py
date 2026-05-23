"""SendMessage dispatch templates for team agent mode.

Each template is a pure function that takes explicit context kwargs and returns
the message string the orchestrating agent's TeamTools.send_message wrapper
delivers to a team member. Required kwargs are validated at call time — a
missing required kwarg raises ValueError immediately so the dispatch failure
is loud and local, not silent and downstream.

Templates correspond 1:1 with the Phase 1-5c dispatch points in
scripts/team_executor.py. The executor imports and uses them; the wire
protocol is documented in team_executor.py's module docstring.

The 10 templates:
  - phase_1_agenda(subject, cycle_n) -> str
  - phase_2_preanalysis(agenda_items, participant) -> str
  - phase_3_open(proposals) -> str
  - phase_3_debate() -> str
  - phase_3_close(proposals, agreements) -> str
  - phase_4_implementer(item, wave_n) -> str
  - phase_5b_ci_failure(wave_n, failed_checks, results) -> str   # NEW, was inlined
  - phase_5b_prime_reviewer(iter_n, action_items, prior_findings=None) -> str
  - phase_5b_prime_fix(finding) -> str
  - phase_5b_prime_pm_acceptance(findings, iter_n) -> str
"""

from __future__ import annotations

from typing import Any

from scripts.fix_loop import Finding


def _require(name: str, value: Any, type_: type) -> None:
    """Validate a required kwarg is present, well-typed, and non-empty.

    Raises ValueError with a clear, locator-friendly message naming the kwarg
    and (for type mismatches) both the expected and observed type+value. Empty
    containers (list/dict/str of length 0) are rejected because every template
    that takes a container would otherwise emit a degenerate brief (e.g. a
    pre-analysis prompt asking the participant to address NO items). Numeric
    types (int/bool) are NOT length-checked — `iter_n=0` is a legal value.
    """
    if value is None:
        raise ValueError(f"dispatch_templates: required kwarg {name!r} is missing")
    if not isinstance(value, type_):
        raise ValueError(
            f"dispatch_templates: kwarg {name!r} must be {type_.__name__}, "
            f"got {type(value).__name__}={value!r}"
        )
    if isinstance(value, (list, dict, str)) and len(value) == 0:
        raise ValueError(
            f"dispatch_templates: required kwarg {name!r} is empty (got empty {type_.__name__})"
        )


def phase_1_agenda(*, subject: str | None, cycle_n: int) -> str:
    """Phase 1 PM agenda brief. `subject` may be None (PM-directed)."""
    _require("cycle_n", cycle_n, int)
    # subject can be None — represents "PM-directed" cycles.
    return (
        f"Kaizen cycle {cycle_n} — Phase 1 (Agenda). "
        f"Subject: {subject or 'PM-directed'}. "
        "Propose 1-5 agenda items, one per line. Prefix 'ABANDON:' if you "
        "cannot in good faith produce any useful agenda for this cycle."
    )


def phase_2_preanalysis(*, agenda_items: list[str], participant: str) -> str:
    """Phase 2 pre-analysis brief for one non-PM participant."""
    _require("agenda_items", agenda_items, list)
    _require("participant", participant, str)
    bullets = "\n".join(f"- {item}" for item in agenda_items)
    return (
        f"Phase 2 (Pre-analysis). You are {participant}. "
        f"Agenda from PM:\n{bullets}\n\n"
        "Produce a short proposal touching each item from your domain lens. "
        "Prefix 'ABANDON:' to opt out."
    )


def phase_3_open(*, proposals: list[dict]) -> str:
    """Phase 3 Star open: broadcast every Phase-2 proposal to a participant."""
    _require("proposals", proposals, list)
    summary_lines = [f"- {p['agent']}: {p['raw'][:200]}" for p in proposals]
    body = "\n".join(summary_lines) if summary_lines else "(no proposals collected)"
    return (
        "Phase 3 open (Synthesis meeting — Star). All Phase-2 proposals "
        f"below; read them and prepare your debate position:\n{body}"
    )


def phase_3_debate() -> str:
    """Phase 3 Mesh debate brief. Stateless — no kwargs."""
    return (
        "Phase 3 debate (Mesh). State your remaining concerns and your "
        "agreed scope for this cycle. Prefix 'ABANDON:' if no consensus "
        "is reachable from your seat."
    )


def phase_3_close(*, proposals: list[dict], agreements: list[dict]) -> str:
    """Phase 3 Star close: PM consolidates into the Action Items DAG."""
    _require("proposals", proposals, list)
    _require("agreements", agreements, list)
    return (
        "Phase 3 close (Star). Consolidate the proposals and the agreed "
        f"scope into a single Action Items DAG. Proposals: {len(proposals)}; "
        f"agreements: {len(agreements)}. "
        "Reply with one fenced ```json``` block containing a JSON list of "
        "Action Item dicts. Each dict must have keys: id (str), touches "
        "(list[str]), reads (list[str]), depends_on (list[str]), "
        "wave (int), owner (str role id). "
        "Prefix 'ABANDON:' if no DAG can be agreed."
    )


def phase_4_implementer(*, item: dict, wave_n: int) -> str:
    """Phase 4 brief for the owner of one Action Item in wave `wave_n`."""
    _require("item", item, dict)
    _require("wave_n", wave_n, int)
    return (
        f"Phase 4 wave {wave_n} — implement Action Item {item['id']}. "
        f"You own this item. Touches: {item.get('touches')}; "
        f"reads: {item.get('reads')}. Apply the change to disk in the "
        "clone and reply with a one-line summary of what you did. "
        "Prefix 'ABANDON:' if the change cannot be applied."
    )


def phase_5b_ci_failure(
    *, wave_n: int, failed_checks: list[str], results: dict | None = None
) -> str:
    """Phase 5b CI-failure routing detail message used in abandonment.

    Extracted from the inline CI-failure abandonment branch in
    team_executor.py Phase 4. The returned string is byte-identical to
    cycle 1's inline emission ``f"CI failed after wave {wave_n}: {failed}"``
    so the wire protocol does not drift. `results` is accepted (and
    validated when present) for callers that want to log the full check
    map, but it is NOT included in the returned string.
    """
    _require("wave_n", wave_n, int)
    _require("failed_checks", failed_checks, list)
    if results is not None and not isinstance(results, dict):
        raise ValueError(
            "dispatch_templates: kwarg 'results' must be dict or None, "
            f"got {type(results).__name__}={results!r}"
        )
    return f"CI failed after wave {wave_n}: {failed_checks}"


def phase_5b_prime_reviewer(
    *,
    iter_n: int,
    action_items: list[dict],
    prior_findings: list[Finding] | None = None,
) -> str:
    """Build the reviewer brief for iteration `iter_n`.

    On iteration 1 (or when `prior_findings` is None/empty) the brief is the
    fresh-review form. On iteration 2+ the brief carries forward the
    previously-unresolved findings so reviewers can do incremental review
    rather than re-scanning the whole diff from scratch.
    """
    _require("iter_n", iter_n, int)
    _require("action_items", action_items, list)
    # prior_findings is optional (may be None) — only validate when present.
    if prior_findings is not None and not isinstance(prior_findings, list):
        raise ValueError(
            "dispatch_templates: kwarg 'prior_findings' must be list or None, "
            f"got {type(prior_findings).__name__}"
        )
    ids = [item["id"] for item in action_items]
    base = (
        f"Phase 5b' iteration {iter_n} — independent review. "
        f"Review the implemented Action Items: {ids}. Reply with either "
        "'NO ISSUES' (case-insensitive) OR one finding per line in the "
        "format: [severity] file:line — text  "
        "(severity ∈ blocker|major|minor|nit)."
    )
    if not prior_findings:
        return base
    # Render the carry-forward block so iteration 2+ reviewers can do
    # incremental review against the previous round's surviving findings.
    prior_lines = [
        f"  - {f.finding_id} [{f.severity}] {f.reviewer} @ {f.file_line}: {f.finding}"
        for f in prior_findings
    ]
    prior_block = "\n".join(prior_lines)
    return (
        f"{base}\n\nPreviously unresolved findings (iteration {iter_n - 1}); "
        f"verify whether the implementer's fix attempts resolved each:\n{prior_block}"
    )


def phase_5b_prime_fix(*, finding: Finding) -> str:
    """Phase 5b' fix brief: dispatch a single finding to its implementer."""
    _require("finding", finding, Finding)
    return (
        f"Phase 5b' fix — address finding {finding.finding_id} "
        f"({finding.severity}) at {finding.file_line}: {finding.finding}. "
        "Apply the fix and reply with a one-line confirmation. Prefix "
        "'ABANDON:' if the fix cannot be applied."
    )


def phase_5b_prime_pm_acceptance(*, findings: list[Finding], iter_n: int) -> str:
    """Ask the PM whether the unresolved findings are acceptable for this cycle.

    Per internal/cycle/SKILL.md the PM may rule remaining issues acceptable
    (a legitimate fix-loop exit). Reply must start with ACCEPT or REJECT.
    """
    _require("findings", findings, list)
    _require("iter_n", iter_n, int)
    finding_lines = [
        f"  - {f.finding_id} [{f.severity}] {f.reviewer} @ {f.file_line}: {f.finding}"
        for f in findings
    ]
    body = "\n".join(finding_lines) if finding_lines else "  (none)"
    return (
        f"Phase 5b' PM acceptance check (iteration {iter_n}). "
        f"The reviewers surfaced these findings:\n{body}\n\n"
        "As PM, do you accept them as out-of-scope for THIS cycle (we will "
        "log them for follow-up), or do we keep iterating? Reply starting "
        "with ACCEPT or REJECT, followed by a one-line rationale."
    )
