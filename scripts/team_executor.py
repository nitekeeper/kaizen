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
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from scripts._tmux_workspace import apply_workspace_layout, set_pane_title, set_pane_titles
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
    phase_5b_prime_fix,
    phase_5b_prime_pm_acceptance,
    phase_5b_prime_reviewer,
    phase_5d_shutdown,
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
    tree, skipping the usual transient/VCS directories.

    F4 (audit cleanup): previously, an OSError during rglob silently
    returned an empty frozenset — which then made the DAG validator
    surface every action item's `reads` as "unsatisfiable" because the
    file set was empty. The abandonment then misattributed the cause to
    "unsatisfiable reads" when the real problem was a permissions/IO
    error walking the clone. Now an OSError is re-raised with a clearer
    message naming the path and the original error so triage isn't
    misdirected. The "clone doesn't exist yet" case is still tolerated by
    the explicit `exists()` check above.
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
    except OSError as exc:
        # F4: re-raise with a clearer message so the abandonment caller can
        # surface "the walk itself failed" instead of "reads unsatisfiable."
        raise OSError(f"rglob failed on {root}: {exc}") from exc
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


# ── Post-spawn tmux helpers ────────────────────────────────────────────────
#
# kaizen launches teammates into a tmux workspace via the Agent Teams API.
# CC titles every team-mode pane ``general-purpose`` (the default
# subagent_type) and lays them out in the tiled layout — which makes
# multi-wave cycles hard to follow at a glance. We reshape the workspace
# into "main pane left + right-side 2-column grid" and retitle each pane
# with its current wave + role.
#
# ONCE PER CYCLE: layout is applied a single time, right after the Phase 2
# fan-out completes (when CC's lazy-spawn has materialised every
# teammate's pane). Re-applying ``select-layout main-vertical`` would
# undo the join-pane folding, so we gate the call on a one-shot flag.
#
# PER WAVE: only titles change. We re-use the pane_id → agent map
# returned by ``apply_workspace_layout`` to retitle the wave's owners to
# ``[w{wave_n}] {role}`` without touching pane geometry.
#
# Tmux interactions are best-effort: a "no server running" / missing
# workspace returns an empty map and the cycle proceeds without any tmux
# decoration. Issue kaizen#55 is the original report.

_MAIN_AGENT_PREFERENCE = ("agent-systems-architect-1", "software-architect-1", "pm-1")


def _apply_pane_label(
    recipient: str,
    desired_title: str,
    current_title: dict[str, str],
    pane_to_agent: dict[str, str],
) -> bool:
    """Idempotent per-pane retitle, gated on whether the desired label changed.

    Returns ``True`` iff a tmux retitle call was actually issued. The
    decision predicate is "the recipient's last-applied label is NOT
    equal to ``desired_title``" — so a Phase 5b' reviewer respawn whose
    pane is currently labeled ``[w3] sdet-1`` retitles to
    ``[R2] sdet-1`` even though the recipient is "already in" the
    layout's initial role-name dictionary.

    This replaces the older one-shot ``titled_recipients: set[str]``
    pattern (R1-2 fix, kaizen#61): under the prior pattern, any
    recipient added to the set at layout time would silently skip every
    subsequent retitle even if the desired label changed across wave /
    reviewer iterations. The dict-of-labels form lets the predicate
    look at the actual currently-applied label rather than a binary
    "have we ever titled this pane" flag.

    Tolerances (mirror :func:`scripts._tmux_workspace.set_pane_title`):
      - empty ``pane_to_agent`` → return False (tmux unavailable or
        layout hasn't been applied yet);
      - recipient absent from ``pane_to_agent`` → return False
        (positional zip mismatched the roster, or CC reordered panes);
      - ``set_pane_title`` itself soft-fails on no-server tmux, so the
        update to ``current_title`` is best-effort but never raises.

    The ``current_title`` dict is mutated in place when a retitle call
    is issued — the caller MUST pass the same dict across all
    invocations within one cycle so the label-change predicate sees a
    consistent view.
    """
    if current_title.get(recipient) == desired_title:
        return False
    if not pane_to_agent:
        return False
    agent_to_pane = {agent: pid for pid, agent in pane_to_agent.items()}
    pid = agent_to_pane.get(recipient)
    if pid is None:
        return False
    set_pane_title(pid, desired_title)
    current_title[recipient] = desired_title
    return True


def _pick_main_agent(roster: list[str]) -> str:
    """Return the first agent from ``_MAIN_AGENT_PREFERENCE`` in ``roster``.

    Falls back to the first roster member when none of the preferred
    architect/PM roles are present. Returns ``""`` if ``roster`` is empty
    — the layout helper treats that as "do not swap, leave default."
    """
    for candidate in _MAIN_AGENT_PREFERENCE:
        if candidate in roster:
            return candidate
    return roster[0] if roster else ""


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
    # GAP-7 — eager `team_members` capture was wrong (MAJOR-4 of fix-loop
    # iteration 2): CC's Agent spawn is LAZY on first SendMessage, so
    # capturing the team_create members list at the top would dispatch
    # ghost shutdowns to never-spawned roles when a Phase 1 abandon
    # happens immediately after team_create.
    #
    # Instead, track `active_members` as a SET, populated lazily — append
    # a recipient only after `tools.send_message` / `send_message_many`
    # returns successfully. We wrap the injected `tools` in a lightweight
    # local proxy (`TrackedTools`) that does the recording; the rest of
    # the executor uses `tools` (the proxy) unchanged. The finally block
    # iterates the lazy set, so a cycle that abandoned before any
    # SendMessage fired produces an empty `active_members` and the
    # shutdown step is skipped entirely. Test (d) covers the empty case.
    active_members: set[str] = set()

    class _TrackedTools:
        """Local proxy: forwards every method to the injected `tools`
        wrapper. `send_message` and `send_message_many` additionally
        record the recipient(s) in the enclosing-scope `active_members`
        set AFTER a successful return — exceptions on the underlying
        call do NOT mark the recipient active (no spawn happened).

        Per-method delegation is intentional: a `__getattr__` proxy would
        bypass the Protocol-shape preflight that ran on the ORIGINAL
        `tools` above.
        """

        def __init__(self, inner) -> None:
            self._inner = inner

        def team_create(self, name: str, members: list[str]) -> str:
            return self._inner.team_create(name, members)

        def send_message(self, team_id: str, to: str, message: str) -> str:
            resp = self._inner.send_message(team_id, to, message)
            active_members.add(to)
            # kaizen#61 — per-spawn retitle hook. Apply tmux layout +
            # title this recipient's pane on first observation.
            _retitle_on_first_send(to)
            return resp

        def send_message_many(self, messages: list[dict]) -> list[str]:
            resps = self._inner.send_message_many(messages)
            # Record every recipient whose call did not raise. The batch
            # is all-or-nothing on the wrapper side (a single exception
            # aborts the whole batch), so on a successful return every
            # `to` in the batch is active.
            for m in messages:
                active_members.add(m["to"])
                # kaizen#61 — retitle each batch recipient on first
                # observation. Set-membership keeps repeats cheap.
                _retitle_on_first_send(m["to"])
            return resps

        def team_delete(self, team_id: str) -> None:
            self._inner.team_delete(team_id)

    raw_tools = tools
    tools = _TrackedTools(raw_tools)

    team_id = tools.team_create(team_name, members=list(roster) if roster else [pm])

    # Tmux workspace setup — populated lazily as teammates are spawned by
    # SendMessage (CC's team-mode spawn is lazy, not at TeamCreate time).
    # See _MAIN_AGENT_PREFERENCE comment block above for the layout
    # rationale.
    pane_to_agent: dict[str, str] = {}
    # kaizen#61 / R1-2 (Phase 5b' major): replace the prior one-shot
    # ``titled_recipients: set[str]`` with a label-tracking dict so the
    # retitle predicate compares the CURRENT pane label against the
    # desired one. The earlier set form silently swallowed every retitle
    # request for a recipient that had ever been labeled — Phase 5b'
    # reviewer respawns would keep their Phase 4 ``[w{n}]`` label (or
    # bare role for non-implementer reviewers) so the operator could
    # not visually distinguish a fix-loop iteration from a past wave.
    # See :func:`scripts.team_executor._apply_pane_label` for the
    # decision predicate.
    current_title: dict[str, str] = {}
    # One-shot "did I fold yet" flag (Phase 4 wave dispatch retitles but
    # MUST NOT re-fold — re-folding would undo earlier join-pane work).
    layout_applied: list[bool] = [False]

    def _setup_tmux_layout_once() -> None:
        if layout_applied[0]:
            return
        layout_applied[0] = True
        try:
            result = apply_workspace_layout(
                workspace_name=team_name,
                ordered_agents=list(roster) if roster else [pm],
                main_agent=_pick_main_agent(list(roster) if roster else [pm]),
            )
        except Exception as exc:  # pragma: no cover - defensive
            _log.warning("apply_workspace_layout failed: %s", exc)
            return
        pane_to_agent.update(result)
        if pane_to_agent:
            # Pre-wave initial titles: bare role names, no wave prefix
            # yet. Phase 4 overwrites these with ``[w{n}] {role}`` per wave;
            # Phase 5b' overwrites with ``[R{iter_n}] {role}`` per round.
            try:
                set_pane_titles(team_name, dict(pane_to_agent))
            except Exception as exc:  # pragma: no cover - defensive
                _log.warning("initial set_pane_titles failed: %s", exc)
            # Record each role's CURRENT label (bare role name) so the
            # next retitle predicate can compare against the desired
            # label and decide whether a tmux call is needed.
            for role in pane_to_agent.values():
                current_title[role] = role

    def _retitle_on_first_send(recipient: str) -> None:
        """Apply layout (if not yet) and label the recipient's pane.

        Called after every successful ``send_message`` /
        ``send_message_many`` from ``_TrackedTools``. The label-change
        predicate inside :func:`_apply_pane_label` makes this cheap
        on repeat calls — a recipient already labeled with its bare
        role name is a one-comparison no-op.

        Phase 4 and Phase 5b' wrap a different desired label
        (``[w{n}] {role}`` / ``[R{iter_n}] {role}``) so the per-spawn
        bare-role label here is only authoritative until the first
        wave/iter dispatch overrides it.
        """
        if not layout_applied[0]:
            _setup_tmux_layout_once()
        try:
            _apply_pane_label(recipient, recipient, current_title, pane_to_agent)
        except Exception as exc:  # pragma: no cover - defensive
            _log.warning("per-spawn retitle for %s failed: %s", recipient, exc)

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
                # All teammate panes exist by now — apply the workspace
                # layout once (issue kaizen#55).
                _setup_tmux_layout_once()
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

        # F10 (audit cleanup): capture a CI baseline BEFORE wave 1 dispatch
        # so the per-wave diff can tell "the cycle introduced this break"
        # apart from "the host arrived with this break." We use ``"true"``
        # as the test command so the baseline's pytest call is a no-op
        # exit-0 — we want the lint/security/sca baseline, not a baseline
        # pytest run (which would be unfaithful to the cycle's eventual
        # post-wave invocation that uses the project's real test_command).
        ci_baseline: dict[str, dict] | None = None
        if not outcome_acc.abandoned and waves:
            try:
                _baseline_passed, ci_baseline = run_ci_checks(clone_dir, "true")
            except Exception as baseline_exc:
                # A baseline crash is non-fatal — log and proceed without
                # a baseline (every fail will be "cycle-introduced," same
                # as the pre-F10 behavior).
                _log.warning("CI baseline run failed: %s — proceeding without diff", baseline_exc)
                ci_baseline = None
        if not outcome_acc.abandoned and waves:
            items_by_id = {item["id"]: item for item in action_items}
            for wave_n, wave_ids in enumerate(waves, start=1):
                # Collect the wave's owners up-front so we can retitle
                # their panes for this wave. Order mirrors wave_ids so
                # the visual order matches the DAG order.
                wave_owners = [items_by_id[ai_id].get("owner") or pm for ai_id in wave_ids]
                # Layout was applied once after Phase 2. Per wave we only
                # retitle the wave's owners' panes (issue kaizen#55).
                # Tolerant of "no server running" / missing pane — never raises.
                _setup_tmux_layout_once()
                # R1-2: route the wave-prefix retitle through the same
                # `_apply_pane_label` predicate the per-spawn hook uses,
                # so the `current_title` dict stays the single source of
                # truth for "what label is on each pane right now."
                for name in wave_owners:
                    try:
                        _apply_pane_label(
                            name,
                            f"[w{wave_n}] {name}",
                            current_title,
                            pane_to_agent,
                        )
                    except Exception as tmux_exc:  # pragma: no cover - defensive
                        _log.warning(
                            "wave-%s retitle for %s failed: %s",
                            wave_n,
                            name,
                            tmux_exc,
                        )
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
                    # F10 (audit cleanup): diff against the pre-wave-1
                    # baseline so a pre-existing host failure does NOT
                    # abandon the cycle. Only cycle-introduced failures
                    # are unrecoverable; pre-existing ones are logged.
                    cycle_introduced, pre_existing = _diff_ci_results(ci_baseline, results)
                    if pre_existing:
                        # Pre-existing failures: log to stderr (structured
                        # logging is out of scope) but do not abandon.
                        print(
                            f"[team_executor] wave {wave_n} CI: ignoring pre-existing "
                            f"failures from baseline: {pre_existing}",
                            file=sys.stderr,
                        )
                    if not cycle_introduced:
                        # All failures were pre-existing → cycle continues.
                        continue
                    # F12 (audit cleanup): map the highest-severity failed
                    # category to a per-CI-kind reason rather than always
                    # using `tests_unrecoverable`. Detail names both
                    # cycle-introduced and pre-existing for triage.
                    reason = _pick_highest_reason(cycle_introduced)
                    detail = (
                        f"CI failed after wave {wave_n}: "
                        f"cycle-introduced={cycle_introduced}, "
                        f"pre-existing={pre_existing}"
                    )
                    _abandon(
                        outcome_acc,
                        phase_reached="test",
                        reason=reason,
                        detail=detail,
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
                        # R1-2: retitle reviewer panes to ``[R{iter_n}] {role}``
                        # so the operator can visually distinguish a
                        # fix-loop iteration from a past Phase 4 wave.
                        # Without this, a reviewer that wasn't a Phase 4
                        # implementer keeps its bare role-name label; a
                        # reviewer that WAS keeps its last ``[w{wave_n}]``
                        # label (both confusing). The `_apply_pane_label`
                        # predicate fires the retitle iff the desired
                        # label actually differs from the current one.
                        for reviewer in reviewers:
                            try:
                                _apply_pane_label(
                                    reviewer,
                                    f"[R{iter_n}] {reviewer}",
                                    current_title,
                                    pane_to_agent,
                                )
                            except Exception as tmux_exc:  # pragma: no cover - defensive
                                _log.warning(
                                    "Phase 5b' iter-%s reviewer retitle for %s failed: %s",
                                    iter_n,
                                    reviewer,
                                    tmux_exc,
                                )
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
                        # R1-2: retitle PM pane with the iter label so
                        # the operator sees ``[R{n}] pm-1`` while the
                        # acceptance prompt is in flight.
                        try:
                            _apply_pane_label(
                                pm,
                                f"[R{iter_n}] {pm}",
                                current_title,
                                pane_to_agent,
                            )
                        except Exception as tmux_exc:  # pragma: no cover - defensive
                            _log.warning(
                                "Phase 5b' iter-%s PM retitle failed: %s", iter_n, tmux_exc
                            )
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
                            # R1-2: retitle the fix recipient to the
                            # current iteration label. A teammate that
                            # was last labeled as Phase 4 implementer
                            # (``[w{n}] {role}``) now reads ``[R{n}] {role}``
                            # while the fix dispatch is in flight.
                            try:
                                _apply_pane_label(
                                    fix_owner,
                                    f"[R{iter_n}] {fix_owner}",
                                    current_title,
                                    pane_to_agent,
                                )
                            except Exception as tmux_exc:  # pragma: no cover - defensive
                                _log.warning(
                                    "Phase 5b' iter-%s fix retitle for %s failed: %s",
                                    iter_n,
                                    fix_owner,
                                    tmux_exc,
                                )
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
            # F13 (audit cleanup): check=True would raise CalledProcessError
            # with no captured stdout/stderr in the message, masking the
            # real problem (clone is corrupt, HEAD missing, etc). Use
            # check=False and assert explicitly so the error names the
            # actual exit code and stderr.
            rev = subprocess.run(
                ["git", "-C", str(clone_dir), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
            )
            if rev.returncode != 0:
                raise RuntimeError(
                    f"git rev-parse HEAD in {clone_dir} exited "
                    f"{rev.returncode}: {(rev.stderr or rev.stdout or '').strip()}"
                )
            commit_sha = rev.stdout.strip()
            if not commit_sha:
                raise RuntimeError(
                    f"git rev-parse HEAD in {clone_dir} returned an empty SHA; "
                    "the clone may be corrupt or HEAD may be unset."
                )
    finally:
        # GAP-7 (docs/kaizen/2026-05-24-bridge-smoke-3.md) — graceful
        # teammate shutdown BEFORE team_delete. Per CC's TeamCreate docs:
        # "Gracefully terminate teammates first, then call TeamDelete after
        # all teammates have shut down." Each spawned teammate must approve
        # a shutdown_request → its CC process terminates → TeamDelete
        # succeeds without orphan members.
        #
        # Fire-and-proceed semantics (architect-approved trade-off, fix-loop
        # iteration 2): we do NOT parse `send_message_many`'s return values.
        # If a teammate replied approve=false or didn't reply at all, we
        # still call team_delete below — CC will either succeed (most cases)
        # or fail with active-members; the latter falls through to the
        # existing leaked-team sweep path on next run. The simplicity of
        # fire-and-proceed is worth more than a couple of extra orphan rows
        # to inspect manually.
        #
        # `active_members` is populated lazily by the `_TrackedTools` proxy
        # above (MAJOR-4 fix): only roles that actually received a
        # successful send_message / send_message_many appear here. A cycle
        # that abandoned before any SendMessage fired has an empty set, so
        # the shutdown step is skipped and we go straight to team_delete.
        if active_members:
            try:
                # Iterate in stable sorted order so test assertions
                # (and any future debug logs) are deterministic; the
                # underlying set is unordered.
                shutdown_messages = [
                    {
                        "team_id": team_id,
                        "to": member,
                        # Fresh uuid per call (default arg) — each
                        # request_id is unique so a teammate cannot
                        # confuse two concurrent requests.
                        "message": phase_5d_shutdown(),
                    }
                    for member in sorted(active_members)
                ]
                tools.send_message_many(shutdown_messages)
            except Exception as exc:
                # Don't block team_delete on shutdown failure. Log + proceed.
                _log.warning(
                    "GAP-7 shutdown send_message_many failed for team %s: %s. "
                    "Proceeding with team_delete; orphans may need next-run sweep.",
                    team_id,
                    exc,
                )
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
