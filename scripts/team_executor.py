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

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from scripts._tmux_config import check_glyph_readiness
from scripts._tmux_workspace import (
    KAIZEN_TEAM_ID_OPTION,
    PANE_LABEL_PREFIX_RE,
    apply_workspace_layout,
    set_pane_title,
    set_pane_titles,
)
from scripts.agent_id_match import guarded_argv, team_agent_id_regex
from scripts.caveman_codec import compress as _caveman_compress
from scripts.caveman_codec import should_compress as _caveman_should_compress
from scripts.cc_tool_bridge import quorum_for
from scripts.ci_runner import parse_pytest_pass_count, run_ci_checks
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

    def send_message_many(
        self, messages: list[dict], *, quorum_floor: int | None = None
    ) -> list[str]:
        """Batch dispatch — enqueue N messages in parallel; return their
        responses in input order. Each dict has ``team_id``, ``to``,
        ``message``. Used by Phase 2 fan-out, Phase 3 Star-open broadcast,
        and Phase 5b' parallel reviewer dispatch — see
        docs/kaizen/2026-05-24-bridge-smoke-2.md GAP-4 for the motivation
        (sequential send_message is the wall-clock bottleneck of a cycle).

        ``quorum_floor`` (#83): None → strict (every row must reply). An int
        opts into quorum-relaxed dispatch (silent stragglers soft-dropped once
        quorum is met and the per-row soft-timeout elapses). Safety-critical
        phases (reviewers, state-mutating gates) MUST leave it None.
        """
        ...

    def team_delete(self, team_id: str) -> None:
        """Tear down the team."""
        ...

    def apply_layout(self, team_id: str) -> None:
        """Fold the orchestrator's tmux window into the PM-left + 2-col grid
        (kaizen#86). Best-effort + cosmetic. In bridge mode the implementation
        enqueues an `apply_layout` request the orchestrator services in the
        window-owning session; in-process the fold can't reach that window."""
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


# ── B2 — caveman compression at a MODEL-BOUND post-parse prose sink ───────
#
# Gate env var: KAIZEN_CAVEMAN_COMPRESS. Parsed defensively (mirrors the
# CYCLE_WALL_S / EXPERIMENTAL_AGENT_TEAMS posture):
#   unset / empty / "0" / "false" (any case)  → OFF   (M8: DEFAULT OFF, opt-in)
#   "1" / "true" (any case) / other truthy     → ON
#
# DEFAULT = OFF (M8) — opt-in until efficacy is measured. This env-gate covers
# ONLY the codec mutator (:func:`compress_reply_for_context`). The B1
# `_TERSE_OUTPUT_RULE` prompt injection in dispatch_templates is a SEPARATE,
# always-on lever and is NOT gated by this var.
#
# The ONLY place the codec is applied is :func:`compress_reply_for_context`,
# wired at the Phase-3 Star-open broadcast (B2): the Phase-2 proposal PROSE
# that kaizen concatenates back INTO a prompt every roster agent reads. It is
# applied POST-PARSE (`_is_abandon` already ran on the raw Phase-2 text) and to
# a SEPARATE copy — the stored `proposals` list stays byte-exact, so the codec
# NEVER touches bytes a parser (`_parse_reviewer_response`,
# `_parse_action_items`, `_parse_agenda_items`, `_is_abandon`), the DB, or git
# reads. When the gate is OFF (the default), the helper returns its input
# UNCHANGED (byte-identical), so OFF fully restores prior behavior.
_CAVEMAN_ENV = "KAIZEN_CAVEMAN_COMPRESS"


def _caveman_enabled() -> bool:
    """Return True iff the caveman-compress codec sink is ON.

    DEFAULT OFF (M8) — opt-in. Unset / empty / ``"0"`` / ``"false"`` / ``"no"``
    / ``"off"`` (any case, whitespace-trimmed) ⇒ OFF; only an explicit truthy
    value (``"1"`` / ``"true"`` / anything else non-falsey) turns it ON.
    Mirrors the defensive parsing of the other kaizen env flags.
    """
    raw = os.environ.get(_CAVEMAN_ENV)
    if raw is None:
        return False  # unset → default OFF (M8)
    val = raw.strip().lower()
    # Explicit truthy markers → ON; falsey / unrecognised → OFF.
    return val in ("1", "true", "yes", "on")


def compress_reply_for_context(text: str, level: str = "full") -> str:
    """Compress MODEL-BOUND prose for re-broadcast into a downstream prompt.

    This is the B2 sink — the SOLE place :mod:`scripts.caveman_codec` is wired
    into the live cycle path. It is applied ONLY to free-text that flows back
    INTO a dispatch prompt an agent reads (the Phase-3 Star-open proposal
    broadcast), on a SEPARATE copy. It is NEVER applied to:

    - text fed to the byte-sensitive parsers (`_parse_reviewer_response`,
      `_parse_action_items`, `_parse_agenda_items`, `_is_abandon`) — those
      receive RAW reply text upstream of this call;
    - the stored ``"raw"`` proposal field (kept byte-exact);
    - structured fields stored in the DB or consumed by git / PR rendering.

    Behavior:
    - Feature gate OFF (:func:`_caveman_enabled`, the DEFAULT per M8) ⇒ return
      ``text`` unchanged (byte-identical — OFF fully restores prior behavior).
    - Auto-clarity tripwire (:func:`caveman_codec.should_compress` is False —
      security / destructive / multi-step prose) ⇒ return ``text`` unchanged
      (pass through verbatim; the codec must not flip meaning).
    - Otherwise return :func:`caveman_codec.compress(text, level)`.

    Non-string / empty input is returned unchanged.
    """
    if not isinstance(text, str) or text == "":
        return text
    if not _caveman_enabled():
        return text
    if not _caveman_should_compress(text):
        return text
    return _caveman_compress(text, level)


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


# ── kaizen#98: activity-glyph readiness preflight ──────────────────────────
#
# At team-mode start we run a one-shot, advisory ``check_glyph_readiness``
# (scripts._tmux_config) so a run launched on a STALE marker or with
# ``allow-set-title off`` in effect logs WHY the live Claude idle/busy glyph
# is not rendering, instead of silently showing no glyph. Never fatal.


def _tmux_conf_path() -> Path:
    """Return the tmux.conf path to check (mirrors setup.py's locator).

    Prefers ``~/.tmux.conf``; falls back to ``~/.config/tmux/tmux.conf`` only
    when THAT exists and the canonical one does not. Returns the canonical
    path when neither exists (the file check tolerates a missing file).
    """
    home = Path(os.path.expanduser("~"))
    canonical = home / ".tmux.conf"
    xdg = home / ".config" / "tmux" / "tmux.conf"
    if canonical.exists():
        return canonical
    if xdg.exists():
        return xdg
    return canonical


def _live_allow_set_title() -> str | None:
    """Best-effort read of the running server's global ``allow-set-title``.

    Returns the value string (e.g. ``"off"`` / ``"on"``) or ``None`` when
    tmux is absent, no server is running, or the value can't be read. Never
    raises — the glyph-readiness check degrades to the file-only check.
    """
    if shutil.which("tmux") is None:
        return None
    try:
        proc = subprocess.run(  # nosec - argv is a fixed list, no shell=True
            ["tmux", "show-options", "-gv", "allow-set-title"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_CLEANUP_SUBPROC_TIMEOUT_S,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() or None


# ── kaizen#68: OS-level teammate cleanup (belt-and-suspenders) ─────────────
#
# CC's TeamDelete is session-scoped: it removes the in-session team registry
# and `~/.claude/teams/<team_id>/` config dir, but it does NOT signal the
# spawned `claude --agent-id <name>@<team_name>` teammate processes. When the
# shutdown_request handshake also fails to terminate them (run 35 cycle 3
# postmortem — `{"type":"shutdown_request"}` returned approve=true at the
# CC tool layer but the underlying process kept running), the teammate
# processes and their tmux panes survive indefinitely.
#
# `_cleanup_team_artifacts` runs at the end-of-cycle finally block (BEFORE
# `tools.team_delete()`) and performs four layers of cleanup, each
# idempotent and tolerant of partial state:
#
#   L1 — pgrep verify shutdown actually terminated each teammate.
#   L2 — pkill -TERM then -KILL any survivor (scoped to this team_name).
#   L3 — tmux kill-pane any pane whose TITLE matches a teammate role-id
#         (primary), with pid/argv probe as a fallback. The orchestrator's
#         own pane (TMUX_PANE) is explicitly excluded.
#   L4 — verify (and fallback rm -rf) `~/.claude/teams/<team_id>/` after
#         tools.team_delete() runs (called by the finally block).
#         Note: the CC on-disk dir is keyed by team_id (UUID), NOT
#         team_name (cf. scripts/cleanup_orphans.py + sweep_leaked_teams).
#
# Naming note: `team_name` is the human-readable label kaizen passes to
# TeamCreate (e.g. `kaizen-cycle-35-3`) and is what appears in the
# `--agent-id <role>@<team_name>` argv of every spawned teammate. `team_id`
# is the UUID-shaped opaque handle returned by TeamCreate and is what CC
# uses for `~/.claude/teams/<team_id>/`. The two MUST NOT be confused.
#
# L3 design (MAJOR-1 from the kaizen#68 fix-loop iteration 2): the
# original implementation matched panes by `pane_pid`, which on the
# maintainer's box is the bash shell that wraps `claude`, not claude
# itself. The empirical evidence in the issue body is exactly this:
# claude reaped (good), bash shell pane lingers (bad). The fix matches
# panes by `pane_title`, which PR #70's `@desired_title` machinery
# pins to the teammate's role-id (`backend-engineer-1`, `arch-1`, …).
# Pid/argv match is retained as a secondary probe for the case where
# OSC 2 from the pane process strips the title back to
# `general-purpose` (per the `project-kaizen-run-37-pane-identity`
# memory).
#
# All subprocess calls use fixed argv (no shell=True), check=False
# (idempotency: pkill returns nonzero when no processes match — that's
# success for us), and short timeouts so a hung subprocess cannot stall
# cycle teardown. The team_name is `re.escape`-d defensively even though
# kaizen team names are restricted to `kaizen-cycle-<run>-<n>` — defense
# in depth against future name changes.

# Module-level seams so tests can monkeypatch without spawning real
# subprocesses. Production code uses these names exclusively for the
# pgrep / pkill / tmux / ps calls in `_cleanup_team_artifacts`.
_CLEANUP_SHUTDOWN_GRACE_S = 2.5  # delay between shutdown_response and L1 pgrep
_CLEANUP_SIGTERM_GRACE_S = 5.0  # delay between SIGTERM and L2 re-check

# Default subprocess timeout (seconds) for cleanup commands. A hung pgrep
# / pkill / tmux call must NOT block cycle teardown indefinitely — the
# user is waiting to start the next session.
_CLEANUP_SUBPROC_TIMEOUT_S = 10.0

# Field separator for `tmux list-panes -F` output. pane_title may contain
# spaces (e.g. PM pane is "● team-lead / PM"), so `.split()` is unsafe.
# US (unit separator, 0x1f) passes through tmux's format string verbatim
# and never appears in legitimate pane titles after _sanitize_title's
# C0-control strip.
_TMUX_FIELD_SEP = "\x1f"

# One-shot stderr warning gate per missing tool — keeps cleanup quiet
# when re-invoked from idempotent retries while still surfacing the
# "this tool is gone" condition once per process.
_MISSING_TOOL_WARNED: set[str] = set()


def _warn_missing_tool(tool: str) -> None:
    """Emit a one-time stderr warning when ``tool`` is not on PATH.

    Repeated calls within the same process are no-ops — kaizen runs many
    cleanup cycles, and N copies of the same warning would drown the
    operator's real signal. The set is module-level on purpose; per
    `_cleanup_team_artifacts`'s idempotency contract a second cleanup
    call should not re-emit.
    """
    if tool in _MISSING_TOOL_WARNED:
        return
    _MISSING_TOOL_WARNED.add(tool)
    print(
        f"[kaizen-cleanup] {tool} not on PATH — cleanup degraded for the rest of this process",
        file=sys.stderr,
    )


def _agent_id_regex(team_name: str) -> str:
    """Return the pkill/pgrep `-f` regex for processes in ``team_name``.

    The regex matches the literal substring ``--agent-id <name>@<team_name>``
    in the full command line. We anchor on a literal-space-or-end boundary
    after the team_name so a substring match of a longer team_name
    (``kaizen-cycle-5-1`` matching ``kaizen-cycle-5-11``) cannot happen.

    NIT (kaizen#68 iter 2): use ``( |$)`` rather than ``(\\s|$)`` —
    BSD pkill's `-f` does NOT understand ``\\s``, and the only whitespace
    that appears in a real /proc/*/cmdline argv after the team_name is a
    space anyway.

    ``team_name`` is `re.escape`-d defensively even though kaizen team
    names are restricted to ``kaizen-cycle-<run>-<n>`` (no regex metachars
    by construction). Defense in depth against future team-name changes.

    Thin wrapper over :func:`scripts.agent_id_match.team_agent_id_regex` —
    the canonical pattern (and the ``--`` argv guard at the pgrep/pkill call
    sites) now lives in that shared module (kaizen#82).
    """
    return team_agent_id_regex(team_name)


def _pgrep_teammates(team_name: str) -> list[int]:
    """Return PIDs of live teammate processes for ``team_name`` (L1/L2 check).

    Returns an empty list when no processes match (pgrep exits 1) or when
    pgrep is not on PATH. Any unexpected exit is treated as "no info" and
    returns empty — we DO NOT raise from a cleanup helper.
    """
    try:
        proc = subprocess.run(  # nosec - argv is a fixed list, no shell=True
            guarded_argv("pgrep", ["-f"], _agent_id_regex(team_name)),
            capture_output=True,
            text=True,
            check=False,
            timeout=_CLEANUP_SUBPROC_TIMEOUT_S,
        )
    except FileNotFoundError:
        _warn_missing_tool("pgrep")
        return []
    except subprocess.TimeoutExpired:
        _log.warning("kaizen-cleanup: pgrep timed out after %ss", _CLEANUP_SUBPROC_TIMEOUT_S)
        return []
    if proc.returncode not in (0, 1):
        # pgrep exits 1 when nothing matches (success for us). Anything
        # else is "abnormal but inconclusive" — log and return empty.
        _log.warning("kaizen-cleanup: pgrep exited %s: %s", proc.returncode, proc.stderr.strip())
        return []
    pids: list[int] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pids.append(int(line))
        except ValueError:
            continue
    return pids


def _pkill_teammates(team_name: str, signal: str) -> None:
    """Send ``signal`` (``-TERM`` or ``-KILL``) to teammates of ``team_name``.

    Idempotent: pkill returns 1 when no processes match — that's success
    here (nothing to kill). FileNotFoundError (pkill not on PATH) emits
    a one-time warning and returns. TimeoutExpired is logged and swallowed.
    """
    try:
        subprocess.run(  # nosec - argv is a fixed list, no shell=True
            guarded_argv("pkill", [signal, "-f"], _agent_id_regex(team_name)),
            capture_output=True,
            text=True,
            check=False,
            timeout=_CLEANUP_SUBPROC_TIMEOUT_S,
        )
    except FileNotFoundError:
        _warn_missing_tool("pkill")
        return
    except subprocess.TimeoutExpired:
        _log.warning("kaizen-cleanup: pkill %s timed out", signal)
        return


def _tmux_list_panes() -> list[tuple[str, int, str, str]]:
    """Return ``(pane_id, pane_pid, pane_title, kaizen_team_id)`` per pane.

    Returns ``[]`` when tmux is not installed OR the server isn't running
    OR list-panes hits a hard error. Cleanup proceeds without L3 in any
    of those cases.

    Uses US (0x1f) as the field separator because pane_title may contain
    spaces — splitting on space would corrupt titles like "● team-lead /
    PM". The format spec is therefore
    ``#{pane_id}\\x1f#{pane_pid}\\x1f#{pane_title}\\x1f#{@kaizen_team_id}``;
    we split on the same byte. _sanitize_title strips C0 control chars
    from titles BEFORE they ever land in tmux, so 0x1f cannot appear
    inside a legitimate pane_title.

    The last field (``#{@kaizen_team_id}``) is the per-pane user-option
    set by ``scripts._tmux_workspace.tag_pane_team_id`` at workspace
    creation time. Panes that were never tagged (e.g. orchestrator pane,
    panes created outside kaizen, pre-iter-3 cycles still in flight)
    return the empty string for this field, which the cleanup path
    treats as "not one of mine — leave alone". kaizen#68 iter 3
    MAJOR fix.
    """
    try:
        proc = subprocess.run(  # nosec - argv is a fixed list, no shell=True
            [
                "tmux",
                "list-panes",
                "-a",
                "-F",
                (
                    f"#{{pane_id}}{_TMUX_FIELD_SEP}"
                    f"#{{pane_pid}}{_TMUX_FIELD_SEP}"
                    f"#{{pane_title}}{_TMUX_FIELD_SEP}"
                    f"#{{{KAIZEN_TEAM_ID_OPTION}}}"
                ),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=_CLEANUP_SUBPROC_TIMEOUT_S,
        )
    except FileNotFoundError:
        _warn_missing_tool("tmux")
        return []
    except subprocess.TimeoutExpired:
        _log.warning("kaizen-cleanup: tmux list-panes timed out")
        return []
    if proc.returncode != 0:
        return []
    out: list[tuple[str, int, str, str]] = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split(_TMUX_FIELD_SEP)
        # Need at least 4 fields; defensively tolerate >4 (a pane_title
        # that somehow embeds 0x1f — shouldn't happen, _sanitize_title
        # strips C0 — but we prefer "preserve title verbatim" over
        # "ignore the pane"). The kaizen_team_id field is the LAST
        # one tmux emits, so the 0x1f-bearing pane_title would be
        # split into multiple fields; we re-join everything between
        # parts[2] and parts[-1] exclusive into pane_title.
        if len(parts) < 4:
            continue
        pane_id = parts[0]
        try:
            pane_pid = int(parts[1])
        except ValueError:
            continue
        pane_title = _TMUX_FIELD_SEP.join(parts[2:-1])
        kaizen_team_id = parts[-1]
        out.append((pane_id, pane_pid, pane_title, kaizen_team_id))
    return out


def _ps_args(pid: int) -> str:
    """Return the full argv of ``pid`` as a single string, or '' on error.

    Used as the L3 secondary probe: when a pane's title was stripped by
    a CC subagent OSC 2 emit (the kaizen#66/#70 mechanism), we fall back
    to inspecting the pid's argv to decide if it's one of ours.
    """
    try:
        proc = subprocess.run(  # nosec - argv is a fixed list, no shell=True
            ["ps", "-o", "args=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
            timeout=_CLEANUP_SUBPROC_TIMEOUT_S,
        )
    except FileNotFoundError:
        _warn_missing_tool("ps")
        return ""
    except subprocess.TimeoutExpired:
        _log.warning("kaizen-cleanup: ps -p %s timed out", pid)
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def _tmux_kill_pane(pane_id: str) -> bool:
    """tmux kill-pane -t <pane_id>. Returns True on success.

    Tolerant of "tmux not installed" / "no server" / "pane gone" — any
    error returns False without raising. Idempotent (re-killing a
    already-dead pane is a no-op error → False).
    """
    try:
        proc = subprocess.run(  # nosec - argv is a fixed list, no shell=True
            ["tmux", "kill-pane", "-t", pane_id],
            capture_output=True,
            text=True,
            check=False,
            timeout=_CLEANUP_SUBPROC_TIMEOUT_S,
        )
    except FileNotFoundError:
        _warn_missing_tool("tmux")
        return False
    except subprocess.TimeoutExpired:
        _log.warning("kaizen-cleanup: tmux kill-pane %s timed out", pane_id)
        return False
    return proc.returncode == 0


def _team_config_dir(team_id: str) -> Path:
    """Return the expected ``~/.claude/teams/<team_id>/`` path.

    NB (MAJOR-2 from kaizen#68 fix-loop iter 2): keyed by ``team_id``
    (the UUID returned by TeamCreate), NOT ``team_name`` (the human
    label). Confirmed against ``scripts/cleanup_orphans.py`` and
    ``scripts/sweep_leaked_teams.py``: both treat ``team_id`` as the
    canonical directory key.
    """
    return Path.home() / ".claude" / "teams" / team_id


def _sleep(seconds: float) -> None:
    """time.sleep wrapper — module-level so tests can patch it to a no-op."""
    time.sleep(seconds)


def _cleanup_team_artifacts(
    team_name: str,
    *,
    team_id: str | None = None,
    team_role_ids: list[str] | None = None,
    shutdown_was_attempted: bool = True,
) -> dict:
    """Belt-and-suspenders OS-level cleanup of a kaizen team's artifacts.

    Runs three layers (L1-L3); L4 is a separate call after team_delete.
    Each layer is idempotent and tolerant of partial state:

      L1 — pgrep verify teammates terminated after the shutdown_response
            handshake. Any surviving PIDs escalate to L2.
      L2 — pkill -TERM, wait ~5s, re-pgrep, pkill -KILL any survivors.
      L3 — tmux kill-pane each pane that BOTH (a) is tagged with our
            ``team_id`` via the ``@kaizen_team_id`` tmux user-option
            AND (b) is not the orchestrator's own pane. Title-match
            and pid/argv probe are kept as secondary/tertiary defense
            in depth when team_id is supplied but a pane is untagged
            (older mid-cycle restart). When ``team_id`` is ``None``,
            L3 degrades to the iter-2 title + pid/argv heuristic
            (no cross-team safety — caller is asserting "I am the only
            kaizen run").

    Args:
        team_name: the kaizen-side human label (e.g. ``kaizen-cycle-35-3``)
            embedded in every teammate's ``--agent-id <role>@<team_name>``
            argv. Used by L1/L2's pgrep/pkill regex and the L3 argv probe.
        team_id: the UUID-shaped opaque handle returned by TeamCreate.
            When set, L3 PRIMARY filter is ``kaizen_team_id == team_id``
            from the pane's ``@kaizen_team_id`` user-option — the
            cross-team safety gate against concurrent kaizen runs
            sharing role-ids. When None, L3 falls back to title +
            pid/argv (legacy iter-2 behaviour, no cross-team safety).
        team_role_ids: the team's roster role-id list (e.g.
            ``["pm-1", "backend-engineer-1", "security-engineer-1"]``).
            Used by L3 secondary title-match (defense in depth behind
            the team_id gate).
        shutdown_was_attempted: when False (e.g. Phase 1 abandon before
            any send_message fired), L1's initial 2.5s grace sleep is
            SKIPPED — there's no shutdown to wait for. We still do an
            immediate pgrep, and if it returns survivors we still
            escalate. This avoids a wasted sleep on every fast-abort.

    Returns a dict describing what each layer did — useful for tests and
    for the observability stderr summary the finally block emits.

    Safety contract:
      - Never raises. A bug in cleanup must not block ``tools.team_delete()``
        or the cycle's outcome dict from being returned.
      - Never kills the orchestrator's own tmux pane.
      - Cross-team safety: with team_id supplied, L3 PRIMARY gate
        ensures orchestrator A's cleanup does NOT touch orchestrator
        B's panes (which share the same role-ids).
      - Pgrep/pkill cross-team safety: the agent-id regex is anchored
        on a literal-space/end-of-string boundary after team_name so
        ``kaizen-cycle-5-1`` cannot accidentally kill
        ``kaizen-cycle-5-11`` teammates.
      - Idempotent: calling twice is a no-op on the second call (no
        survivors, no panes to kill).

    Note: this function deliberately runs AFTER the shutdown_request
    handshake (which lives inline in ``team_cycle_executor``'s finally
    block) and BEFORE ``tools.team_delete()``. L4 is verified AFTER
    ``tools.team_delete()`` returns via the separate
    ``_cleanup_verify_config_dir(team_id)`` helper.
    """
    role_ids = frozenset(team_role_ids or [])
    report: dict = {
        "team_name": team_name,
        "team_id": team_id,
        "l1_survivors": 0,
        "l2_sigterm_sent": 0,
        "l2_sigkill_needed": 0,
        "l3_panes_killed": 0,
        "l3_panes_skipped_orchestrator": 0,
        "l3_panes_skipped_other_team": 0,
        "l3_tmux_available": False,
        "l4_config_dir_cleaned_by_fallback": False,
    }

    # ── L1 — confirm shutdown handshake actually terminated teammates ────
    # MINOR (iter 2): skip the 2.5s grace sleep when no shutdown was
    # attempted (e.g. Phase 1 abandon before any send_message fired).
    # On the happy "shutdown handshake worked" path we still want the
    # sleep so the OS has time to reap the spawned process before we
    # pgrep — otherwise L2 would needlessly escalate to SIGTERM.
    try:
        if shutdown_was_attempted:
            _sleep(_CLEANUP_SHUTDOWN_GRACE_S)
        survivors = _pgrep_teammates(team_name)
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning("kaizen-cleanup L1 failed for %s: %s", team_name, exc)
        survivors = []
    report["l1_survivors"] = len(survivors)
    if survivors:
        print(
            f"[kaizen-cleanup] layer 1: {len(survivors)} teammate processes still "
            f"alive after shutdown_response (team={team_name})",
            file=sys.stderr,
        )

    # ── L2 — escalate to OS-level kill (SIGTERM, wait, then SIGKILL) ────
    sigkill_survivors: list[int] = []
    if survivors:
        try:
            _pkill_teammates(team_name, "-TERM")
            report["l2_sigterm_sent"] = len(survivors)
            _sleep(_CLEANUP_SIGTERM_GRACE_S)
            sigkill_survivors = _pgrep_teammates(team_name)
            if sigkill_survivors:
                _pkill_teammates(team_name, "-KILL")
                report["l2_sigkill_needed"] = len(sigkill_survivors)
        except Exception as exc:  # pragma: no cover - defensive
            _log.warning("kaizen-cleanup L2 failed for %s: %s", team_name, exc)
        print(
            f"[kaizen-cleanup] layer 2: SIGTERM sent to "
            f"{report['l2_sigterm_sent']} processes; "
            f"{report['l2_sigkill_needed']} survived to SIGKILL "
            f"(team={team_name})",
            file=sys.stderr,
        )

    # ── L3 — tmux pane cleanup ──────────────────────────────────────────
    try:
        panes = _tmux_list_panes()
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning("kaizen-cleanup L3 list-panes failed: %s", exc)
        panes = []
    # NB: an empty `panes` list does NOT necessarily mean "tmux
    # unavailable" — it could also mean "tmux server is up but no panes
    # exist on it" (rare but possible if the cycle's session was
    # destroyed mid-cleanup). We can't reliably distinguish without
    # re-probing; the conservative read is "L3 had nothing to do",
    # which the summary line below conveys.
    report["l3_tmux_available"] = bool(panes)

    orchestrator_pane = os.environ.get("TMUX_PANE", "").strip()
    pane_match_re = re.compile(_agent_id_regex(team_name))
    # L1/L2 survivor PIDs feed the secondary pid-match path.
    teammate_pids: set[int] = set(survivors) | set(sigkill_survivors)
    panes_to_kill: list[str] = []
    for pane_id, pane_pid, pane_title, pane_team_id in panes:
        if orchestrator_pane and pane_id == orchestrator_pane:
            # Triple-asserted: NEVER kill the orchestrator's pane.
            report["l3_panes_skipped_orchestrator"] += 1
            continue
        # PRIMARY gate (kaizen#68 iter 3 MAJOR fix) — cross-team safety.
        # With ``team_id`` supplied, the only panes we kill are those
        # tagged with @kaizen_team_id == team_id at workspace creation
        # time. A concurrent kaizen orchestrator's panes carry a
        # different @kaizen_team_id and are explicitly skipped.
        if team_id:
            if pane_team_id == team_id:
                panes_to_kill.append(pane_id)
                continue
            if pane_team_id and pane_team_id != team_id:
                # Tagged by a DIFFERENT kaizen run — explicitly skip.
                report["l3_panes_skipped_other_team"] += 1
                continue
            # Untagged pane (pane_team_id == ""). Fall through to the
            # secondary defense-in-depth probes ONLY for panes we have
            # other evidence are ours (title-match or argv-match). An
            # untagged pane with NO other evidence is left alone —
            # cross-team safety is the dominant concern.
        # SECONDARY — pane_title is a role-id from the team's roster.
        # Defense in depth: when team_id is None (legacy callers) or
        # when a pane somehow escaped tagging at workspace creation,
        # the title-match still catches it. We still skip if the pane
        # has a DIFFERENT team_id tag — that gate ran above.
        if role_ids:
            stripped_title = PANE_LABEL_PREFIX_RE.sub("", pane_title or "", count=1)
            if stripped_title in role_ids or pane_title in role_ids:
                panes_to_kill.append(pane_id)
                continue
        # TERTIARY — by observed teammate PID (kept for forward-
        # compat if CC ever spawns claude as the direct pane process).
        if pane_pid in teammate_pids:
            panes_to_kill.append(pane_id)
            continue
        # QUATERNARY probe — `ps -o args= -p <pane_pid>` for the case
        # where the pane runs claude directly (no wrapper shell) AND
        # its title was stripped by an OSC 2 from the pane process.
        argv = _ps_args(pane_pid)
        if argv and pane_match_re.search(argv):
            panes_to_kill.append(pane_id)
    for pane_id in panes_to_kill:
        if _tmux_kill_pane(pane_id):
            report["l3_panes_killed"] += 1
    # MINOR (iter 2): always emit the L3 summary so the operator can
    # distinguish "tmux unavailable" (l3_tmux_available=False) from
    # "tmux up, zero teammate panes" (l3_tmux_available=True, killed=0).
    # iter 3: also surface skipped-other-team count when non-zero.
    print(
        f"[kaizen-cleanup] layer 3: killed {report['l3_panes_killed']} tmux panes "
        f"(team={team_name}, team_id={team_id!r}, "
        f"tmux_available={report['l3_tmux_available']}, "
        f"skipped_other_team={report['l3_panes_skipped_other_team']})",
        file=sys.stderr,
    )

    return report


def _cleanup_verify_config_dir(team_id: str) -> bool:
    """L4 — verify (and fallback rm -rf) ``~/.claude/teams/<team_id>/``.

    Called AFTER ``tools.team_delete()`` returns. ``team_delete`` is
    supposed to remove the directory; this layer is the fallback for the
    rare case where it doesn't (per the issue body and runbook).

    Args:
        team_id: the UUID-shaped opaque handle returned by TeamCreate.
            NB (MAJOR-2 from iter 2): NOT the team_name. The on-disk
            convention is ``~/.claude/teams/<team_id>/``, confirmed
            against scripts/cleanup_orphans.py + sweep_leaked_teams.

    Returns True iff the fallback removal fired (i.e. team_delete left
    the directory behind). False on the happy path AND on every safety-
    refusal path.

    Safety guards (kaizen#68 iter 3 MINOR — defense in depth):
      - Empty / falsy ``team_id`` is refused. An empty string would
        resolve to ``~/.claude/teams`` (no leaf) and a naive rmtree
        would wipe EVERY kaizen team's config dir on the box.
      - ``team_id`` containing path separators (``/`` or ``\\``) or
        ``..`` (parent traversal) is refused — these could redirect
        the rmtree to an arbitrary path. Unreachable from kaizen
        callers (team_id always comes from CC's TeamCreate response),
        but rmtree of a wrong path is irreversible so the defense is
        cheap and warranted.
      - Final assertion: the resolved cfg_dir's basename MUST equal
        ``team_id`` — anything else means our `_team_config_dir` was
        monkeypatched in a way the guard upstream didn't catch.
    """
    if not team_id or "/" in team_id or "\\" in team_id or ".." in team_id:
        _log.warning("kaizen-cleanup L4 refusing empty/path-shaped team_id=%r", team_id)
        return False
    cfg_dir = _team_config_dir(team_id)
    if cfg_dir.name != team_id:
        # Defense in depth: the path's leaf MUST be team_id. If
        # _team_config_dir was monkeypatched to return a wholly
        # different path (which tests sometimes do for fixtures), we
        # accept the test's intent there. But if the leaf doesn't
        # match team_id at all, we refuse.
        _log.warning(
            "kaizen-cleanup L4 path leaf mismatch (cfg_dir=%s, team_id=%r); refusing",
            cfg_dir,
            team_id,
        )
        return False
    if not cfg_dir.exists():
        print(
            f"[kaizen-cleanup] layer 4: config dir cleaned (team_id={team_id})",
            file=sys.stderr,
        )
        return False
    # Fallback removal — TeamDelete left the dir behind.
    print(
        f"[kaizen-cleanup] layer 4: config dir {cfg_dir} still exists after "
        f"team_delete — applying fallback rm -rf (team_id={team_id})",
        file=sys.stderr,
    )
    shutil.rmtree(cfg_dir, ignore_errors=True)
    return True


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
        call do NOT mark the recipient active. (kaizen#96: a pane MAY have
        spawned before a BridgeTimeoutError — the lazy spawn precedes the
        poll — but we still keep `active_members` success-only so the GAP-7
        shutdown handshake only targets bridge-serviced slots; an orphaned
        spawned pane is reaped by the team-NAME-matched L1/L2 process
        cleanup (pgrep/pkill on the teammate ``--agent-id role@team_name``
        argv) + the always-fires team_delete. (A lazily-spawned pane is
        never ``@kaizen_team_id``-tagged — tagging is a one-shot snapshot
        taken at layout time, before the Phase-2 panes exist — so it falls
        THROUGH the L3 ``team_id`` gate, but the L1/L2 process sweep catches
        it regardless.) The tmux RE-FOLD, by contrast, DOES run on the
        raising path — see the `finally` blocks below.)

        Per-method delegation is intentional: a `__getattr__` proxy would
        bypass the Protocol-shape preflight that ran on the ORIGINAL
        `tools` above.
        """

        def __init__(self, inner) -> None:
            self._inner = inner

        def team_create(self, name: str, members: list[str]) -> str:
            return self._inner.team_create(name, members)

        def send_message(self, team_id: str, to: str, message: str) -> str:
            # kaizen#96 — the retitle + re-fold MUST run even if the inner
            # send raises. CC's team-mode spawn is lazy on first SendMessage,
            # so by the time a BridgeTimeoutError fires the recipient's pane
            # has ALREADY materialised and tmux has auto-retiled the window
            # into a single column; if we skip the fold on the raising path
            # that collapse becomes permanent (run 55 / bridge.db row 720).
            # The retitle/fold keys off the INPUT ``to`` (not the response),
            # so it is valid to run in ``finally``; the bare ``finally``
            # re-raises the original exception automatically.
            try:
                resp = self._inner.send_message(team_id, to, message)
                # active-member contract is INTENTIONALLY success-only
                # (kaizen#96): ``active_members`` gates the GAP-7 graceful
                # shutdown handshake, and we only want to handshake a slot
                # the bridge actually serviced. A pane that spawned-then-
                # timed-out is still reaped by the team-NAME-matched L1/L2
                # process cleanup (pgrep/pkill on the teammate
                # ``--agent-id role@team_name`` argv) + the always-fires
                # team_delete in the cycle ``finally`` — so leaving it out of
                # ``active_members`` does not orphan it, and keeping it out
                # preserves the "no active on exception" contract the
                # lifecycle tests pin.
                active_members.add(to)
                return resp
            finally:
                # kaizen#61 — per-spawn retitle hook. Apply tmux layout +
                # title this recipient's pane on first observation.
                # kaizen#88 — clear the per-batch re-fold flag, retitle (which
                # sets it iff `to` is a new pane), then fold ONCE after —
                # UNLESS a phase boundary owns the fold
                # (``suppress_batch_refold``), in which case the boundary's
                # single unconditional fold covers this spawn and we must not
                # add a per-message fold.
                needs_refold[0] = False
                _retitle_on_first_send(to)
                if needs_refold[0] and not suppress_batch_refold[0]:
                    _request_orchestrator_fold()
                needs_refold[0] = False

        def send_message_many(
            self, messages: list[dict], *, quorum_floor: int | None = None
        ) -> list[str]:
            # kaizen#96 — the post-fan-out re-fold MUST survive an inner
            # raise. ``send_message_many`` is what lazily SPAWNS the batch's
            # panes (tmux auto-retiles to a single column); the kaizen#88
            # re-fold then restores the grid. In run 55, ``_poll_many`` raised
            # ``BridgeTimeoutError`` at the 600s PER_CALL_TIMEOUT_S AFTER the
            # panes had spawned, so the inner call left this wrapper BEFORE the
            # re-fold — and with no ``except`` on the cycle ``try`` the
            # single-column collapse became permanent (bridge.db had exactly
            # ONE apply_layout row, emitted before the fan-out). Running the
            # retitle/re-fold in ``finally`` (keyed off the INPUT ``messages``,
            # not the responses) re-folds on BOTH the success and the raising
            # path; the bare ``finally`` re-raises the original exception.
            try:
                resps = self._inner.send_message_many(messages, quorum_floor=quorum_floor)
                # Record every recipient on a SUCCESSFUL return. The batch is
                # all-or-nothing on the wrapper side (a single exception aborts
                # the whole batch), so on success every `to` in the batch is
                # active. NB with a quorum-relaxed batch a soft-dropped
                # recipient still appears here (its slot returned a sentinel
                # response, not an exception) — harmless for active-member
                # tracking, which only gates the GAP-7 shutdown handshake.
                #
                # kaizen#96 — active-member tracking stays SUCCESS-ONLY (not
                # moved into ``finally``): see the rationale on
                # ``send_message`` above. A spawned-then-timed-out pane is
                # reaped by the team-NAME-matched L1/L2 process cleanup
                # (pgrep/pkill on the ``--agent-id role@team_name`` argv) +
                # the always-fires team_delete, so excluding it from
                # ``active_members`` neither orphans it nor regresses the
                # lifecycle "no active on exception" contract.
                for m in messages:
                    active_members.add(m["to"])
                return resps
            finally:
                # kaizen#88 — COALESCE the re-fold to once per batch: a batch
                # that spawns N new panes is a SINGLE pane-count change from
                # the operator's view, so we clear the flag, retitle every
                # recipient (each first-seen one sets `needs_refold`), then
                # fold ONCE after the whole batch — never one fold per message.
                needs_refold[0] = False
                for m in messages:
                    # kaizen#61 — retitle each batch recipient on first
                    # observation. Set-membership keeps repeats cheap.
                    _retitle_on_first_send(m["to"])
                # Fold ONCE after the whole batch — unless a phase boundary
                # owns the fold (``suppress_batch_refold``), per kaizen#88
                # MAJOR-2.
                if needs_refold[0] and not suppress_batch_refold[0]:
                    _request_orchestrator_fold()
                needs_refold[0] = False

        def team_delete(self, team_id: str) -> None:
            self._inner.team_delete(team_id)

        def apply_layout(self, team_id: str) -> None:
            self._inner.apply_layout(team_id)

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
    # One-shot "did I do the title/bookkeeping setup yet" flag. The in-process
    # ``apply_workspace_layout`` + initial ``set_pane_titles`` run EXACTLY once
    # per cycle (re-running them would re-issue the no-op in-process fold and
    # re-stamp bare titles over wave labels). The ORCHESTRATOR-side fold is NOT
    # gated by this — see ``_request_orchestrator_fold`` / ``needs_refold``.
    layout_applied: list[bool] = [False]
    # kaizen#88 — every recipient we have ever dispatched to. CC's team-mode
    # spawn is lazy and per-first-``SendMessage``, so the FIRST time we see a
    # ``to`` a new pane was just materialised in the orchestrator's window,
    # which tmux auto-retiles (collapsing the grid). Tracking "have I seen this
    # recipient" is a FIRST-CONTACT HEURISTIC, NOT a true pane-count delta: it
    # fires on teammate ADD but is blind to teammate REMOVE (TeamDelete of a
    # straggler) and to RESPAWN of an already-seen role. Those cases are
    # backstopped by the UNCONDITIONAL per-phase-boundary fold
    # (``_phase_boundary_fold``) issued at each Phase-4 wave boundary and each
    # Phase-5b' reviewer iteration — that is the backbone that delivers
    # "PM-left + 2-col grid at ALL times"; the heuristic is a within-batch
    # complement so a mid-phase spawn re-folds promptly rather than waiting
    # for the next boundary. (review MAJOR-1 / MINOR-5)
    seen_recipients: set[str] = set()
    # Per-batch "a new pane appeared — re-fold once after this batch" flag.
    # Mutated by ``_retitle_on_first_send``; checked + cleared by the
    # ``_TrackedTools`` wrappers so the re-fold coalesces to ONE
    # ``apply_layout`` per batch (not one per message). A list so the
    # closure can mutate it.
    needs_refold: list[bool] = [False]
    # kaizen#88 (review MAJOR-2) — suppress the per-message new-recipient
    # in-wrapper trigger while a phase BOUNDARY owns the fold. The Phase-4
    # wave loop dispatches owners via individual ``tools.send_message`` calls;
    # without this guard a K-new-owner wave would fire K folds. The wave (and
    # each Phase-5b' iteration) sets this True around its dispatches and emits
    # exactly ONE unconditional ``_phase_boundary_fold`` at the boundary, so
    # the per-wave / per-iteration fold count is exactly 1. A list so the
    # closure can mutate it.
    suppress_batch_refold: list[bool] = [False]

    def _request_orchestrator_fold() -> None:
        """Emit an ``apply_layout`` bridge request so the ORCHESTRATOR folds
        its own window into "PM-left + 2-col grid" (kaizen#86 path).

        UNGATED by design (kaizen#88): the in-process
        ``apply_workspace_layout`` runs in the detached ``run_bridged``
        process, whose tmux commands never reach the orchestrator's window —
        so the fold is a no-op there and must be re-requested every time the
        live pane count changes. The request routes through the IDEMPOTENT
        ``scripts.fold_workspace`` → ``fold_current_window`` (reset-then-fold),
        so firing it repeatedly is safe — never call ``fold_right_column``
        directly here. Best-effort + cosmetic: the wrapper swallows bridge
        errors so a layout request can never abort the cycle.
        """
        try:
            tools.apply_layout(team_id)
        except Exception as exc:  # pragma: no cover - defensive
            _log.warning("apply_layout request failed: %s", exc)

    def _phase_boundary_fold() -> None:
        """Unconditional, idempotent re-fold issued at a phase boundary.

        kaizen#88 (review MAJOR-1): the new-recipient heuristic in
        ``_retitle_on_first_send`` only catches teammate ADD. A teammate
        REMOVE (a straggler reaped between waves) or a RESPAWN of an
        already-seen role also re-tiles the window but produces no new
        recipient — so the heuristic misses it. An unconditional fold at each
        Phase-4 wave boundary and each Phase-5b' reviewer iteration SELF-HEALS
        the grid regardless of WHY the pane count changed, because
        ``fold_current_window`` is reset-then-fold idempotent (a fold when the
        grid is already correct is a cheap no-op).

        Also clears any pending ``needs_refold`` so a within-batch trigger
        that fired during this boundary's dispatch does not double-fold on top
        of this one (bounds the count to exactly 1 per boundary).
        """
        needs_refold[0] = False
        _request_orchestrator_fold()

    def _setup_tmux_layout_once() -> None:
        if layout_applied[0]:
            return
        layout_applied[0] = True
        try:
            result = apply_workspace_layout(
                workspace_name=team_name,
                ordered_agents=list(roster) if roster else [pm],
                main_agent=_pick_main_agent(list(roster) if roster else [pm]),
                # kaizen#68 iter 3 — tag every teammate pane so cleanup
                # can gate destructive actions on team-id equality
                # (cross-team safety against concurrent kaizen runs
                # sharing role-ids).
                team_id=team_id,
            )
        except Exception as exc:  # pragma: no cover - defensive
            _log.warning("apply_workspace_layout failed: %s", exc)
            # Still request the orchestrator-side fold below — the in-process
            # call failing does not mean the orchestrator window cannot fold.
            result = {}
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
        # kaizen#86/#88 — emit the FIRST orchestrator-side fold (the
        # post-first-wave fold). Subsequent spawn waves re-fold via the
        # ``needs_refold`` path in the ``_TrackedTools`` wrappers.
        _request_orchestrator_fold()
        # kaizen#98 Gap B — ONE-SHOT advisory glyph-readiness check at
        # team-mode start (gated by ``layout_applied`` like everything else in
        # this function). Logs WHY the live glyph won't render on a stale /
        # gated config. ADVISORY — never fatal.
        try:
            for warning in check_glyph_readiness(
                _tmux_conf_path(), live_allow_set_title=_live_allow_set_title()
            ):
                _log.warning("kaizen#98 glyph-readiness: %s", warning)
        except Exception as exc:  # pragma: no cover - defensive
            _log.warning("kaizen#98 glyph-readiness check failed: %s", exc)

    def _retitle_on_first_send(recipient: str) -> None:
        """Apply layout (if not yet), label the recipient's pane, and flag a
        re-fold when ``recipient`` is a brand-new pane.

        Called after every successful ``send_message`` /
        ``send_message_many`` from ``_TrackedTools``. The label-change
        predicate inside :func:`_apply_pane_label` makes this cheap
        on repeat calls — a recipient already labeled with its bare
        role name is a one-comparison no-op.

        kaizen#88: a ``recipient`` never seen before means CC just spawned a
        new pane, which tmux auto-retiled — so we SET ``needs_refold`` (the
        ``_TrackedTools`` wrapper folds once after the batch UNLESS a phase
        boundary owns the fold; see ``suppress_batch_refold``). The recipient
        that TRIGGERS the initial setup is not itself flagged, because
        ``_setup_tmux_layout_once`` already issues the initial fold that
        covers it. (In a multi-recipient first batch the later recipients
        still flag a re-fold; that batch-end fold is idempotent — reset-then-
        fold — so the worst case on the very first batch is one redundant but
        harmless fold, never a corrupted grid.)

        This first-contact flag is only a COMPLEMENT to the unconditional
        per-phase-boundary fold (``_phase_boundary_fold``) — it cannot see a
        teammate REMOVE or a RESPAWN, which the boundary fold handles.

        Phase 4 and Phase 5b' wrap a different desired label
        (``[w{n}] {role}`` / ``[R{iter_n}] {role}``) so the per-spawn
        bare-role label here is only authoritative until the first
        wave/iter dispatch overrides it.
        """
        first_setup = not layout_applied[0]
        if first_setup:
            _setup_tmux_layout_once()
        # A never-before-seen recipient → a new pane was just spawned →
        # the grid drifted → request a re-fold. Skip flagging for recipients
        # observed during the very batch that triggered the initial setup
        # fold (that fold already covers them).
        if recipient not in seen_recipients:
            seen_recipients.add(recipient)
            if not first_setup:
                needs_refold[0] = True
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
                            agenda_items=agenda_items,
                            participant=participant,
                            codegraph_available=os.environ.get("KAIZEN_CODEGRAPH_AVAILABLE") == "1",
                        ),
                    }
                    for participant in phase_2_recipients
                ]
                # #83: Phase 2 pre-analysis is a FUNGIBLE fan-out — synthesis
                # is valid on a quorum of proposals, so opt into quorum-relaxed
                # dispatch (a single silent specialist is soft-dropped, not a
                # whole-batch failure). Quorum counts genuine replies only.
                phase_2_responses = tools.send_message_many(
                    phase_2_messages, quorum_floor=quorum_for(len(phase_2_messages))
                )
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
                    # The RAW reply is stored verbatim into `proposals`. It is
                    # never parsed (only `len(proposals)` reaches
                    # `phase_3_close`), but it IS the source for the Phase 3
                    # Star-open broadcast below — see B2 sink there, which
                    # compresses a SEPARATE copy of this prose for the model-
                    # bound broadcast while leaving this stored copy byte-exact.
                    proposals.append({"agent": participant, "raw": resp or ""})

        # ── Phase 3 — Synthesis meeting (Star → Mesh → Star) ──────────────
        action_items: list[dict] = []
        waves: tuple[tuple[str, ...], ...] = ()
        if not outcome_acc.abandoned:
            # Star open: brief every roster member with the proposals
            # GAP-4: batch-dispatch — one transaction, N parallel briefs.
            #
            # B2 — MODEL-BOUND caveman sink. The Phase-3 Star-open prompt
            # broadcasts each Phase-2 proposal's PROSE back into a prompt that
            # every roster agent READS. This is the one genuinely model-bound
            # prose-broadcast sink in the subagent path. We compress a SEPARATE
            # copy of each proposal's `raw` here (POST-PARSE: `_is_abandon` has
            # already run on the raw text in Phase 2) — the stored `proposals`
            # list is left byte-exact, so nothing parsed/DB/git-bound is
            # touched. `compress_reply_for_context` is env-gated + auto-clarity
            # gated, so a protected-token-only proposal comes through verbatim.
            if roster:
                proposals_for_broadcast = [
                    {"agent": p["agent"], "raw": compress_reply_for_context(p["raw"])}
                    for p in proposals
                ]
                # #83: Star-open broadcast is a fungible fan-out — quorum-relaxed.
                tools.send_message_many(
                    [
                        {
                            "team_id": team_id,
                            "to": participant,
                            "message": phase_3_open(proposals=proposals_for_broadcast),
                        }
                        for participant in roster
                    ],
                    quorum_floor=quorum_for(len(roster)),
                )

            # Mesh (simplified): each participant signals consensus.
            # GAP-4: batch-dispatch as well — same all-to-all fan-out shape
            # as Star-open; serialising it was the same Phase 2 bottleneck.
            agreements: list[dict] = []
            if roster:
                # #83: Mesh debate is a fungible fan-out — quorum-relaxed.
                debate_responses = tools.send_message_many(
                    [
                        {
                            "team_id": team_id,
                            "to": participant,
                            "message": phase_3_debate(),
                        }
                        for participant in roster
                    ],
                    quorum_floor=quorum_for(len(roster)),
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
        # Captured at each wave-boundary CI gate; Phase 5c parses the real
        # pytest pass count from the LAST gate's output for the commit message.
        last_ci_results: dict | None = None
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
                # kaizen#88 MAJOR-1/MAJOR-2 — the wave dispatches its owners as
                # individual ``send_message`` calls; suppress the per-message
                # in-wrapper re-fold for the duration of the wave and emit
                # EXACTLY ONE unconditional ``_phase_boundary_fold`` at the
                # boundary (in the finally). This both coalesces the add-case
                # to one fold/wave AND self-heals removals/respawns the
                # first-contact heuristic cannot see. The finally runs even on
                # a mid-wave abandon, so a partially-spawned wave still has its
                # grid restored.
                suppress_batch_refold[0] = True
                try:
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
                                    f"Owner {owner} abandoned AI {ai_id}: "
                                    f"{_abandon_reason(impl_resp)}"
                                ),
                            )
                            wave_abandoned = True
                            break
                        outcome_acc.decisions.append(f"AI {ai_id}: {(impl_resp or '')[:200]}")
                finally:
                    suppress_batch_refold[0] = False
                    _phase_boundary_fold()
                if wave_abandoned:
                    break
                # CI mirror at every wave boundary
                test_command = project.get("test_command") or "pytest"
                all_passed, results = run_ci_checks(clone_dir, test_command)
                last_ci_results = results
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
                        # #83 caller-audit: reviewer dispatch is SAFETY-CRITICAL
                        # (P2/F9) — it stays STRICT (no quorum_floor). A silent
                        # reviewer must escalate to the hard backstop, never be
                        # soft-dropped: soft-dropping a reviewer would collapse
                        # the review->fix loop and let an unreviewed change ship.
                        #
                        # kaizen#88 MAJOR-1 — a reviewer iteration is a phase
                        # boundary: reviewers (and respawned reviewers across
                        # iterations) materialise/re-tile panes. Suppress the
                        # per-batch in-wrapper trigger and emit EXACTLY ONE
                        # unconditional ``_phase_boundary_fold`` after the
                        # reviewer batch (in the finally, so it self-heals even
                        # if a reviewer reply raises). One fold per iteration.
                        suppress_batch_refold[0] = True
                        try:
                            reviewer_responses = tools.send_message_many(reviewer_messages)
                        finally:
                            suppress_batch_refold[0] = False
                            _phase_boundary_fold()
                        for reviewer, resp in zip(reviewers, reviewer_responses, strict=True):
                            # AI-3: the parser receives the RAW reply (`resp`)
                            # — caveman compression NEVER runs upstream of
                            # `_parse_reviewer_response` (the `[severity]
                            # file:line — text` finding grammar is byte-
                            # sensitive). Reviewer prose is NOT re-broadcast
                            # into a downstream prompt, so there is no
                            # model-bound caveman sink here — the parser owns
                            # the raw bytes outright.
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
            # Real pass count from the LAST wave-boundary CI gate. Caveat:
            # this reflects that gate, not a post-fix-loop re-run — acceptable
            # for a commit-message stat.
            n_tests = parse_pytest_pass_count(
                (last_ci_results or {}).get("tests", {}).get("output", "")
            )
            # Minutes live in Memex (docs/kaizen/* is banned from git per
            # CLAUDE.md); reference the Memex slug, mirroring the success dict.
            minutes_ref = f"kaizen:cycle:{run_row['id']}-{cycle_n}"
            commit_cycle(
                clone_dir=clone_dir,
                cycle_n=cycle_n,
                decisions=outcome_acc.decisions or ["team-mode cycle"],
                participants=outcome_acc.participants,
                n_tests=n_tests,
                subject=subject or "team-mode",
                minutes_rel_path=minutes_ref,
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
    except Exception:
        # kaizen#96 Layer B — cycle-level error backstop (the repo owner's
        # explicit ask: "once a layout is established it must remain
        # unchanged / be restored regardless of runtime errors"). Layer A
        # (the ``_TrackedTools`` ``finally`` blocks) already re-folds on the
        # raising-DISPATCH path; this backstop covers ANY other mid-cycle
        # exception (a CI helper raise, DAG parse, commit failure, ...) so a
        # collapsed grid self-heals instead of persisting. It runs in the
        # ``except`` — which fires BEFORE the ``finally``'s graceful shutdown
        # + team_delete — so the re-fold request reaches the orchestrator
        # while the teammate panes still exist. Best-effort and
        # self-contained: it MUST NOT mask or replace the in-flight
        # exception, so the fold is wrapped in its own try/except that logs
        # and continues (defense-in-depth on top of
        # ``_request_orchestrator_fold``'s own swallow), and we always
        # ``raise`` the original error afterwards. Gated on
        # ``layout_applied`` so we only restore a layout that was actually
        # established (nothing to fold if we never got past team_create).
        if layout_applied[0]:
            try:
                _request_orchestrator_fold()
            except Exception as fold_exc:  # pragma: no cover - defensive
                _log.warning("kaizen#96 cycle-level re-fold backstop failed: %s", fold_exc)
        raise
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
                # #83 caller-audit: shutdown is a teardown/state-transition
                # broadcast — STRICT (no quorum_floor). It is already wrapped in
                # best-effort GAP-7 handling below, so a silent member does not
                # block team_delete; quorum-relaxing here would add nothing.
                tools.send_message_many(shutdown_messages)
            except Exception as exc:
                # Don't block team_delete on shutdown failure. Log + proceed.
                _log.warning(
                    "GAP-7 shutdown send_message_many failed for team %s: %s. "
                    "Proceeding with team_delete; orphans may need next-run sweep.",
                    team_id,
                    exc,
                )
        # kaizen#68 — belt-and-suspenders OS-level cleanup (L1-L3) BEFORE
        # team_delete. The shutdown_request handshake above is CC-protocol
        # cleanup; this is OS-level cleanup for the case where the
        # handshake returned success at the tool layer but the actual
        # `claude --agent-id ...` process kept running (run 35 cycle 3
        # postmortem). Cleanup is best-effort and never raises — a bug
        # here must NOT block the team_delete invariant below.
        #
        # Pass the team's role-id roster so L3 can match panes by
        # pane_title (MAJOR-1 fix from iter 2 review). When `active_members`
        # is empty, no shutdown was attempted, so L1 can skip its 2.5s
        # grace sleep (MINOR fix from iter 2 review).
        try:
            _cleanup_team_artifacts(
                team_name,
                team_id=team_id,
                team_role_ids=list(roster) if roster else [pm],
                shutdown_was_attempted=bool(active_members),
            )
        except Exception as exc:  # pragma: no cover - defensive
            _log.warning(
                "kaizen#68 _cleanup_team_artifacts raised for team %s: %s. "
                "Proceeding with team_delete.",
                team_name,
                exc,
            )
        # CRITICAL INVARIANT: team_delete ALWAYS fires — even on exception
        # or abandonment — so the user's Claude Code session does not leak
        # named teams across cycles. Guarded like its neighbors above: a
        # non-BridgeError raised here (e.g. sqlite3.OperationalError from
        # the team registry) must NOT replace the in-flight cycle exception
        # nor skip the L4 verification below.
        try:
            tools.team_delete(team_id)
        except Exception as exc:
            _log.warning(
                "team_delete raised for team %s: %s. The team may be leaked — "
                "next-run sweep: rm -rf ~/.claude/teams/%s/. Proceeding with "
                "L4 config-dir verification.",
                team_id,
                exc,
                team_id,
            )
        # kaizen#68 — L4 verification AFTER team_delete. TeamDelete is
        # supposed to remove ~/.claude/teams/<team_id>/; this fallback
        # handles the rare case where it doesn't. Keyed by team_id (UUID),
        # NOT team_name (MAJOR-2 fix from iter 2 review). Never raises.
        try:
            _cleanup_verify_config_dir(team_id)
        except Exception as exc:  # pragma: no cover - defensive
            _log.warning(
                "kaizen#68 _cleanup_verify_config_dir raised for team_id %s: %s",
                team_id,
                exc,
            )

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
