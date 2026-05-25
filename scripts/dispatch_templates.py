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
  - phase_5b_ci_failure(wave_n, failed_checks) -> str   # NEW, was inlined
  - phase_5b_prime_reviewer(iter_n, action_items, prior_findings=None) -> str
  - phase_5b_prime_fix(finding) -> str
  - phase_5b_prime_pm_acceptance(findings, iter_n) -> str
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from scripts.fix_loop import Finding

# Run-21 GAP-2 fix (see docs/kaizen/2026-05-24-bridge-smoke.md).
#
# In CC team mode, `Agent(team_name=..., name=..., prompt=...)`-spawned
# teammates do NOT auto-relay their spawn-prompt output back to team-lead.
# Recipients must explicitly `SendMessage` their response, or their work
# silently dies (the smoke saw the architect process the brief, go idle,
# and never send anything back — team-lead had to explicitly poke
# "please reply" before any output arrived). Every Phase 1-5c dispatch
# template appends this hard-rule reminder to its body so the requirement
# is impossible for a teammate to miss.
#
# Appended (not prepended) so the agenda content reads naturally — the
# rule is a contract reminder, not the lead-in.
#
# MINOR-2 (fix-loop iteration 2): split into two private sub-constants
# so future edits to the GAP-2 reply contract OR the GAP-7 shutdown
# contract don't risk breaking the other. Public `TEAMMATE_REPLY_RULE` is
# their concatenation; byte-identity goldens still pass because they
# reference the public constant.
#
# Wording notes (fix-loop iteration 1):
#   MAJOR-1: the `to=` argument names the literal string "team-lead" — every
#            team has a registered team-lead agent (the implicit
#            lead_agent_id emitted by TeamCreate). Spelling this out as a
#            copy-pasteable example prevents a literal-minded teammate from
#            guessing a wrong recipient and silently dropping the reply.
#   MAJOR-2: ABANDON-and-stay-silent is a trap — the smoke saw cycles die
#            because a teammate believed `ABANDON: ...` was an exit-without-
#            reply protocol. The rule now states explicitly that abandons
#            ALSO travel via SendMessage with an `ABANDON:` prefixed body.
_REPLY_RULE = (
    "\n\nIMPORTANT — Reply contract: When you complete your task, "
    "you MUST send your response back via "
    'SendMessage(to="team-lead", message=<your reply>). '
    'The `to` value is literally the string "team-lead" — every team '
    "has a registered team-lead agent (the implicit lead_agent_id "
    "emitted by TeamCreate). Do NOT just go idle — in CC team mode, "
    "spawn-prompt output is not auto-relayed, so silent completion means "
    "team-lead never sees your output. Even a brief 'No issues to report' "
    "SendMessage is required to advance the cycle. "
    "Abandon signals also go via SendMessage — start the body with "
    "'ABANDON: <one-line reason>'. Do not skip the SendMessage even "
    "when abandoning."
)

# GAP-7 (2026-05-24, docs/kaizen/2026-05-24-bridge-smoke-3.md) — shutdown
# handshake. CC's TeamCreate tool docs require teammates to be gracefully
# terminated before TeamDelete. The team-lead enqueues a JSON
# `shutdown_request` body to each teammate at cycle end; the teammate
# parses it as a PROTOCOL message and SendMessages back a
# `shutdown_response` body (no prose).
#
# Wording notes (fix-loop iteration 2):
#   MAJOR-1: `message=JSON(...)` was a teammate-confusing pseudo-call —
#            JSON() is not a function in any tool-call syntax. Replaced
#            with a literal JSON STRING in single quotes so a literal-
#            minded LLM can copy-paste it verbatim.
#   MAJOR-2: `<echo from request>` placeholder would have been passed
#            verbatim by a literal-minded teammate. Replaced with explicit
#            "EXACT uuid string from the incoming request's request_id
#            field — copy it verbatim, do NOT alter or wrap it".
#   MAJOR-3: "actively mid-task" was undefined and could deadlock
#            TeamDelete (a teammate that already SendMessaged its phase
#            reply might consider itself still mid-task and approve=false).
#            Now explicitly defined: mid-task = in-flight tool call OTHER
#            than this SendMessage. Having already replied does NOT count;
#            approve=true is the default.
_SHUTDOWN_RULE = (
    " ALSO: if you receive a JSON message body whose first non-whitespace "
    'characters are `{"type":"shutdown_request"`, this is a PROTOCOL '
    "message (NOT a conversational one). Parse it as JSON, extract its "
    "`request_id` field, and respond via SendMessage with a JSON STRING "
    "literal body: "
    'SendMessage(to="team-lead", message=\'{"type":"shutdown_response",'
    '"request_id":"<paste-the-exact-uuid-here>","approve":true}\'). '
    "Set the `request_id` value to the EXACT uuid string from the "
    "incoming request's `request_id` field — copy it verbatim, do NOT "
    "alter, truncate, or wrap it in any other structure. The `message=` "
    "value MUST be a STRING (single-quoted JSON literal as shown), NOT a "
    "dict and NOT a JSON() function call (no such function exists in the "
    "tool-call syntax). Set `approve` to true by default; only set "
    "`approve` to false (with a one-line reason appended to the JSON) if "
    "you are mid-task — where mid-task is DEFINED as: you currently have "
    "an in-flight tool call OTHER than this SendMessage. Having already "
    "replied to your phase prompt does NOT count as mid-task; approve=true "
    "is the default. Approving terminates your process per CC tool "
    "contract. Do NOT respond to a shutdown_request with prose."
)

TEAMMATE_REPLY_RULE = _REPLY_RULE + _SHUTDOWN_RULE

# F9 (audit cleanup): per-phase reply-format suffix used by the two templates
# whose replies REALLY need to surface test/lint status before team-lead can
# proceed (phase_4_implementer and phase_5b_prime_fix). Kept as a separate
# suffix so the global TEAMMATE_REPLY_RULE stays unchanged and the byte-
# identity goldens for the other 8 templates keep passing.
_TESTS_STATUS_REPLY_SUFFIX = (
    "\n\nIMPORTANT — Reply format: your SendMessage body MUST begin with "
    "either `OK:` (change applied cleanly) or `BLOCKED:` (you could not "
    "complete the change). It MUST also include a one-line "
    "`tests: pass | fail | not-run` tag stating whether `pytest` still "
    "passes locally after your edit (use `not-run` only if running pytest "
    "is impossible from where you sit)."
)


def _require(name: str, value: Any, type_: type) -> None:
    """Validate a required kwarg is present, well-typed, and non-empty.

    Raises ValueError with a clear, locator-friendly message naming the kwarg
    and (for type mismatches) both the expected and observed type+value. Empty
    containers (list/dict/str/tuple/set of length 0) are rejected because every
    template that takes a container would otherwise emit a degenerate brief
    (e.g. a pre-analysis prompt asking the participant to address NO items).
    Numeric types (int/bool) are NOT length-checked — `iter_n=0` is a legal
    value.
    """
    if value is None:
        raise ValueError(f"dispatch_templates: required kwarg {name!r} is missing")
    if not isinstance(value, type_):
        raise ValueError(
            f"dispatch_templates: kwarg {name!r} must be {type_.__name__}, "
            f"got {type(value).__name__}={value!r}"
        )
    if isinstance(value, (list, dict, str, tuple, set)) and len(value) == 0:
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
    ) + TEAMMATE_REPLY_RULE


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
    ) + TEAMMATE_REPLY_RULE


def phase_3_open(*, proposals: list[dict]) -> str:
    """Phase 3 Star open: broadcast every Phase-2 proposal to a participant."""
    _require("proposals", proposals, list)
    summary_lines = [f"- {p['agent']}: {p['raw'][:200]}" for p in proposals]
    body = "\n".join(summary_lines) if summary_lines else "(no proposals collected)"
    return (
        "Phase 3 open (Synthesis meeting — Star). All Phase-2 proposals "
        f"below; read them and prepare your debate position:\n{body}"
    ) + TEAMMATE_REPLY_RULE


def phase_3_debate() -> str:
    """Phase 3 Mesh debate brief. Stateless — no kwargs."""
    return (
        "Phase 3 debate (Mesh). State your remaining concerns and your "
        "agreed scope for this cycle. Prefix 'ABANDON:' if no consensus "
        "is reachable from your seat."
    ) + TEAMMATE_REPLY_RULE


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
        "Prefix 'ABANDON:' if no DAG can be agreed. "
        # F8 (audit cleanup): for each file in `touches`, include the
        # corresponding test file in `reads` so implementers in Phase 4
        # can update tests in the same change (mocks-must-match-reality
        # rule: a touched contract must travel with its tests).
        "For each file in `touches`, include any corresponding test file "
        "in `reads` (e.g. `tests/test_X.py` for `src/X.py` or "
        "`scripts/X.py`)."
    ) + TEAMMATE_REPLY_RULE


def phase_4_implementer(*, item: dict, wave_n: int) -> str:
    """Phase 4 brief for the owner of one Action Item in wave `wave_n`."""
    _require("item", item, dict)
    _require("wave_n", wave_n, int)
    return (
        (
            f"Phase 4 wave {wave_n} — implement Action Item {item['id']}. "
            f"You own this item. Touches: {item.get('touches')}; "
            f"reads: {item.get('reads')}. Apply the change to disk in the "
            "clone and reply with a one-line summary of what you did. "
            "Prefix 'ABANDON:' if the change cannot be applied. "
            # F7 (audit cleanup): tell the implementer to list the parent
            # directory and read any prefix/suffix neighbour so the change
            # matches surrounding style (numbered migration files etc).
            "Before editing, list the directory containing each `touches` "
            "path. Read any neighbor file that shares a prefix or suffix "
            "with your target (e.g. `001_*.sql`, `002_*.sql` when touching "
            "`003_*.sql`) so your change matches existing style."
        )
        + TEAMMATE_REPLY_RULE
        + _TESTS_STATUS_REPLY_SUFFIX
    )


def phase_5b_ci_failure(*, wave_n: int, failed_checks: list[str]) -> str:
    """Phase 5b CI-failure routing detail message used in abandonment.

    Extracted from the inline CI-failure abandonment branch in
    team_executor.py Phase 4. The returned string is byte-identical to
    cycle 1's inline emission ``f"CI failed after wave {wave_n}: {failed}"``
    so the wire protocol does not drift.
    """
    _require("wave_n", wave_n, int)
    _require("failed_checks", failed_checks, list)
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
        return base + TEAMMATE_REPLY_RULE
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
    ) + TEAMMATE_REPLY_RULE


def phase_5b_prime_fix(*, finding: Finding) -> str:
    """Phase 5b' fix brief: dispatch a single finding to its implementer."""
    _require("finding", finding, Finding)
    return (
        (
            f"Phase 5b' fix — address finding {finding.finding_id} "
            f"({finding.severity}) at {finding.file_line}: {finding.finding}. "
            "Apply the fix and reply with a one-line confirmation. Prefix "
            "'ABANDON:' if the fix cannot be applied. "
            # F6 (audit cleanup): a finding-driven fix can change a contract
            # that the tests assert on. The implementer must update those
            # tests in the same change and report local pytest status.
            "If your fix changes a contract that tests assert on, update "
            "those tests in the same change. Report whether `pytest` still "
            "passes locally."
        )
        + TEAMMATE_REPLY_RULE
        + _TESTS_STATUS_REPLY_SUFFIX
    )


def phase_5b_prime_pm_acceptance(*, findings: list[Finding], iter_n: int) -> str:
    """Ask the PM whether the unresolved findings are acceptable for this cycle.

    Per internal/cycle/SKILL.md the PM may rule remaining issues acceptable
    (a legitimate fix-loop exit). Reply must start with ACCEPT or REJECT.

    Responses NOT starting with the literal substring ``ACCEPT``
    (case-insensitive, after strip) are treated as REJECT by the executor.
    This includes ``ABANDON:`` prefixes — the PM cannot signal
    cycle-abandonment from this prompt; it only signals accept-or-reject
    for this round's remaining findings. If a participant truly needs to
    abandon the cycle, they do so via their Phase 1/2/3/4 message (where
    the ``ABANDON:`` protocol IS the cycle-abandonment signal).
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
    ) + TEAMMATE_REPLY_RULE


def phase_5d_shutdown(request_id: str | None = None) -> str:
    """Return the JSON-encoded shutdown_request body for the CC SendMessage protocol.

    GAP-7 (2026-05-24, docs/kaizen/2026-05-24-bridge-smoke-3.md) — per the
    CC TeamCreate tool contract, teammates MUST be gracefully terminated
    BEFORE TeamDelete is called. The team-lead enqueues one
    shutdown_request per active teammate; each teammate parses the JSON
    body and responds with a shutdown_response (approve=true unless
    actively mid-task). Approving terminates the teammate process per
    CC's tool contract, so TeamDelete then succeeds without orphan
    members.

    This is a STRUCTURED-JSON message body, NOT a teammate-readable prose
    template. TEAMMATE_REPLY_RULE is NOT appended — the JSON is the
    entire body. The receiving teammate must parse it as a protocol
    message per the SHUTDOWN_BEHAVIOR clause appended to
    TEAMMATE_REPLY_RULE at spawn-prompt time.

    Args:
        request_id: optional UUID; defaults to a fresh uuid4 string.

    Returns:
        JSON string: '{"type":"shutdown_request","request_id":"<uuid>"}'
    """
    if request_id is None:
        request_id = str(uuid.uuid4())
    return json.dumps({"type": "shutdown_request", "request_id": request_id})
