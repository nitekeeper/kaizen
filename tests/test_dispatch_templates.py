"""Tests for scripts/dispatch_templates.py — the Phase 1-5c brief templates.

These tests cover:
  - happy-path: each template produces the expected content (subject, cycle,
    item ids, wave, severity, file:line, etc.) so wire-protocol drift is loud
  - required-kwarg validation: missing kwargs raise ValueError naming the
    kwarg; wrong-type kwargs raise ValueError naming both expected and
    observed types
  - optional-kwarg semantics: phase_1_agenda accepts subject=None;
    phase_5b_prime_reviewer's iter-1 brief omits the carry-forward block,
    iter-2+ includes prior finding text
"""

from __future__ import annotations

import pytest

from scripts.dispatch_templates import (
    TEAMMATE_REPLY_RULE,
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
    phase_5d_shutdown,
)
from scripts.fix_loop import Finding


def _finding(
    *,
    fid: str = "R1-1",
    reviewer: str = "security-engineer-1",
    severity: str = "blocker",
    finding: str = "SQL injection in user query",
    file_line: str = "scripts/run.py:42",
) -> Finding:
    return Finding(
        finding_id=fid,
        reviewer=reviewer,
        severity=severity,
        finding=finding,
        file_line=file_line,
    )


# ── Phase 1 — Agenda ──────────────────────────────────────────────────────


def test_phase_1_agenda_contains_subject_and_cycle_n():
    msg = phase_1_agenda(subject="reduce flakiness", cycle_n=3)
    assert "cycle 3" in msg
    assert "reduce flakiness" in msg
    assert "Phase 1" in msg


def test_phase_1_agenda_handles_None_subject():
    msg = phase_1_agenda(subject=None, cycle_n=1)
    # No crash; falls back to a stable label so the PM understands intent.
    assert "PM-directed" in msg
    assert "cycle 1" in msg


def test_phase_1_agenda_raises_when_cycle_n_missing():
    with pytest.raises(ValueError) as exc:
        phase_1_agenda(subject="x", cycle_n=None)  # type: ignore[arg-type]
    assert "cycle_n" in str(exc.value)


def test_phase_1_agenda_raises_when_cycle_n_wrong_type():
    with pytest.raises(ValueError) as exc:
        phase_1_agenda(subject="x", cycle_n="1")  # type: ignore[arg-type]
    msg = str(exc.value)
    assert "cycle_n" in msg
    assert "int" in msg
    assert "str" in msg


# ── Phase 2 — Pre-analysis ────────────────────────────────────────────────


def test_phase_2_preanalysis_raises_when_agenda_items_missing():
    with pytest.raises(ValueError) as exc:
        phase_2_preanalysis(agenda_items=None, participant="backend-engineer-1")  # type: ignore[arg-type]
    assert "agenda_items" in str(exc.value)


# ── Phase 3 — Synthesis meeting ───────────────────────────────────────────


def test_phase_3_open_includes_proposal_summary():
    msg = phase_3_open(proposals=[{"agent": "be-1", "raw": "switch from foo to bar"}])
    assert "be-1" in msg
    assert "switch from foo to bar" in msg


def test_phase_3_debate_is_stateless():
    msg = phase_3_debate()
    assert "Phase 3 debate" in msg


def test_phase_3_close_includes_proposals_and_agreements():
    msg = phase_3_close(
        proposals=[{"agent": "a", "raw": "x"}, {"agent": "b", "raw": "y"}],
        agreements=[{"agent": "a", "raw": "ok"}],
    )
    assert "Proposals: 2" in msg
    assert "agreements: 1" in msg


# ── Phase 4 — Implementer ─────────────────────────────────────────────────


def test_phase_4_implementer_includes_wave_n_and_item_id():
    item = {"id": "AI-7", "touches": ["foo.py"], "reads": []}
    msg = phase_4_implementer(item=item, wave_n=2)
    assert "wave 2" in msg
    assert "AI-7" in msg


# ── Phase 5b CI failure ───────────────────────────────────────────────────


def test_phase_5b_ci_failure_includes_failed_checks():
    """Byte-identity pin: the returned string MUST match cycle-1's inline
    emission f"CI failed after wave {wave_n}: {failed}" exactly. Drift
    here breaks the wire-protocol invariant the cycle-2 refactor preserves.
    """
    msg = phase_5b_ci_failure(
        wave_n=1,
        failed_checks=["tests", "ruff_check"],
    )
    assert msg == "CI failed after wave 1: ['tests', 'ruff_check']"


def test_phase_5b_ci_failure_rejects_empty_failed_checks():
    """An empty failed_checks list is semantically invalid — if no checks
    failed, the caller should not be invoking the failure template at all.
    """
    with pytest.raises(ValueError) as exc:
        phase_5b_ci_failure(
            wave_n=1,
            failed_checks=[],
        )
    msg = str(exc.value)
    assert "failed_checks" in msg
    assert "empty" in msg


# ── Empty-container rejection (Blocker 2 coverage) ───────────────────────


def test_phase_2_preanalysis_rejects_empty_agenda_items():
    with pytest.raises(ValueError) as exc:
        phase_2_preanalysis(agenda_items=[], participant="backend-engineer-1")
    msg = str(exc.value)
    assert "agenda_items" in msg
    assert "empty" in msg


def test_phase_3_open_rejects_empty_proposals():
    with pytest.raises(ValueError) as exc:
        phase_3_open(proposals=[])
    msg = str(exc.value)
    assert "proposals" in msg
    assert "empty" in msg


@pytest.mark.parametrize(
    "proposals,agreements,expected_kwarg",
    [
        ([], [{"agent": "a", "raw": "ok"}], "proposals"),
        ([{"agent": "a", "raw": "x"}], [], "agreements"),
    ],
)
def test_phase_3_close_rejects_empty_proposals_and_empty_agreements(
    proposals, agreements, expected_kwarg
):
    with pytest.raises(ValueError) as exc:
        phase_3_close(proposals=proposals, agreements=agreements)
    msg = str(exc.value)
    assert expected_kwarg in msg
    assert "empty" in msg


def test_phase_5b_prime_pm_acceptance_rejects_empty_findings():
    with pytest.raises(ValueError) as exc:
        phase_5b_prime_pm_acceptance(findings=[], iter_n=2)
    msg = str(exc.value)
    assert "findings" in msg
    assert "empty" in msg


# ── Phase 5b' Reviewer ────────────────────────────────────────────────────


def test_phase_5b_prime_reviewer_iter1_omits_previously_unresolved():
    msg = phase_5b_prime_reviewer(
        iter_n=1,
        action_items=[{"id": "A"}, {"id": "B"}],
        prior_findings=None,
    )
    assert "iteration 1" in msg
    assert "Previously unresolved" not in msg


def test_phase_5b_prime_reviewer_iter2_includes_prior_findings():
    prior = [_finding(fid="R1-1", finding="missing input validation")]
    msg = phase_5b_prime_reviewer(
        iter_n=2,
        action_items=[{"id": "A"}],
        prior_findings=prior,
    )
    assert "iteration 2" in msg
    assert "Previously unresolved" in msg
    assert "R1-1" in msg
    assert "missing input validation" in msg


# ── Phase 5b' Fix ─────────────────────────────────────────────────────────


def test_phase_5b_prime_fix_includes_severity_and_file_line():
    f = _finding(
        severity="major",
        file_line="scripts/foo.py:117",
        finding="off-by-one in loop bound",
    )
    msg = phase_5b_prime_fix(finding=f)
    assert "major" in msg
    assert "scripts/foo.py:117" in msg
    assert "off-by-one in loop bound" in msg


# ── Phase 5b' PM acceptance ───────────────────────────────────────────────


def test_phase_5b_prime_pm_acceptance_explains_accept_reject_protocol():
    msg = phase_5b_prime_pm_acceptance(
        findings=[_finding()],
        iter_n=3,
    )
    assert "ACCEPT" in msg
    assert "REJECT" in msg
    assert "iteration 3" in msg


# ── Item 2: PM-ABANDON semantics docstring + body protocol ───────────────


def test_phase_5b_prime_pm_acceptance_docstring_specifies_ABANDON_treated_as_REJECT():
    """Item 2: docstring must explicitly state that ABANDON: prefixes from
    the PM at the acceptance prompt are treated as REJECT (not as cycle
    abandonment). The body must also tell the PM about the ACCEPT/REJECT
    protocol so the agent has the contract in-message.
    """
    assert phase_5b_prime_pm_acceptance.__doc__ is not None
    assert "ABANDON" in phase_5b_prime_pm_acceptance.__doc__
    assert "REJECT" in phase_5b_prime_pm_acceptance.__doc__
    msg = phase_5b_prime_pm_acceptance(
        findings=[
            Finding(
                finding_id="F-1",
                reviewer="r",
                severity="blocker",
                finding="x",
                file_line="a.py:1",
            )
        ],
        iter_n=2,
    )
    assert "ACCEPT" in msg and "REJECT" in msg


# ── Item 5: _require empty-container rejection extended to tuple & set ───


def test_require_rejects_empty_tuple():
    """Item 5: empty tuples must be rejected by `_require` with the same
    'empty' substring in the error message that empty list/dict/str use.
    """
    from scripts.dispatch_templates import _require

    with pytest.raises(ValueError) as exc:
        _require("x", (), tuple)
    msg = str(exc.value)
    assert "x" in msg
    assert "empty" in msg


def test_require_rejects_empty_set():
    """Item 5: empty sets must be rejected by `_require` with the same
    'empty' substring in the error message that empty list/dict/str use.
    """
    from scripts.dispatch_templates import _require

    with pytest.raises(ValueError) as exc:
        _require("x", set(), set)
    msg = str(exc.value)
    assert "x" in msg
    assert "empty" in msg


# ── Run-21 GAP-2: every teammate-dispatch template appends TEAMMATE_REPLY_RULE ─


# Minimum-valid-kwargs builders for each teammate-dispatch template.
# phase_5b_ci_failure is intentionally excluded — it formats an
# abandonment-outcome detail string, NOT a teammate-bound SendMessage body.
_TEAMMATE_DISPATCH_TEMPLATES = [
    ("phase_1_agenda", lambda: phase_1_agenda(subject="x", cycle_n=1)),
    (
        "phase_2_preanalysis",
        lambda: phase_2_preanalysis(agenda_items=["a"], participant="p"),
    ),
    ("phase_3_open", lambda: phase_3_open(proposals=[{"agent": "a", "raw": "x"}])),
    ("phase_3_debate", lambda: phase_3_debate()),
    (
        "phase_3_close",
        lambda: phase_3_close(
            proposals=[{"agent": "a", "raw": "x"}],
            agreements=[{"agent": "a", "raw": "y"}],
        ),
    ),
    (
        "phase_4_implementer",
        lambda: phase_4_implementer(
            item={"id": "A", "touches": ["f.py"], "reads": []},
            wave_n=1,
        ),
    ),
    (
        "phase_5b_prime_reviewer",
        lambda: phase_5b_prime_reviewer(
            iter_n=1,
            action_items=[{"id": "A"}],
            prior_findings=None,
        ),
    ),
    (
        "phase_5b_prime_reviewer_iter2",
        lambda: phase_5b_prime_reviewer(
            iter_n=2,
            action_items=[{"id": "A"}],
            prior_findings=[
                Finding(
                    finding_id="R1-1",
                    reviewer="r",
                    severity="blocker",
                    finding="x",
                    file_line="a.py:1",
                )
            ],
        ),
    ),
    (
        "phase_5b_prime_fix",
        lambda: phase_5b_prime_fix(
            finding=Finding(
                finding_id="R1-1",
                reviewer="r",
                severity="blocker",
                finding="x",
                file_line="a.py:1",
            )
        ),
    ),
    (
        "phase_5b_prime_pm_acceptance",
        lambda: phase_5b_prime_pm_acceptance(
            findings=[
                Finding(
                    finding_id="R1-1",
                    reviewer="r",
                    severity="blocker",
                    finding="x",
                    file_line="a.py:1",
                )
            ],
            iter_n=1,
        ),
    ),
]


@pytest.mark.parametrize(
    "name,builder", _TEAMMATE_DISPATCH_TEMPLATES, ids=[n for n, _ in _TEAMMATE_DISPATCH_TEMPLATES]
)
def test_every_template_appends_teammate_reply_rule(name, builder):
    """Run-21 GAP-2 (+ fix-loop iteration 1): every teammate-bound dispatch
    template MUST append TEAMMATE_REPLY_RULE to its message body.

    The rule is appended (not prepended) so the agenda content reads
    naturally — the reminder lives at the end. We assert:
      1. `TEAMMATE_REPLY_RULE.strip()` appears in the rendered body
         (the rule is present at all)
      2. The body ENDS with TEAMMATE_REPLY_RULE (appended, not prepended
         or interleaved)
      3. The literal `to="team-lead"` recipient example appears in the
         appended rule (MAJOR-1: prevents teammates from guessing a wrong
         relational recipient like "team-lead@<team-name>" or "pm-1")
      4. The ABANDON clause appears in the appended rule (MAJOR-2:
         teammates that abandon must STILL SendMessage with an
         `ABANDON:`-prefixed body — silent-abandonment was the GAP-2
         failure mode the smoke surfaced)
    """
    msg = builder()
    assert TEAMMATE_REPLY_RULE.strip() in msg, (
        f"{name}: TEAMMATE_REPLY_RULE.strip() not found in rendered body"
    )
    # F9 (audit cleanup): phase_4_implementer and phase_5b_prime_fix append
    # an EXTRA per-phase reply-format suffix AFTER TEAMMATE_REPLY_RULE so
    # team-lead always sees an `OK:`/`BLOCKED:` + `tests:` tag on reply. The
    # global rule still appears verbatim — it just isn't the trailing block.
    from scripts.dispatch_templates import _TESTS_STATUS_REPLY_SUFFIX

    if name in ("phase_4_implementer", "phase_5b_prime_fix"):
        assert msg.endswith(TEAMMATE_REPLY_RULE + _TESTS_STATUS_REPLY_SUFFIX), (
            f"{name}: rendered body must end with TEAMMATE_REPLY_RULE + the "
            "F9 per-phase suffix — last 300 chars: " + repr(msg[-300:])
        )
    else:
        assert msg.endswith(TEAMMATE_REPLY_RULE), (
            f"{name}: rendered body must END with TEAMMATE_REPLY_RULE "
            "(append, not prepend) — last 200 chars: " + repr(msg[-200:])
        )
    # MAJOR-1: literal copy-pasteable `to="team-lead"` recipient example.
    assert 'to="team-lead"' in msg, (
        f'{name}: appended rule must include literal `to="team-lead"` '
        "recipient example (MAJOR-1 of fix-loop iteration 1)"
    )
    # MAJOR-2: ABANDON-also-via-SendMessage clause.
    assert "ABANDON" in msg and "SendMessage" in msg, (
        f"{name}: appended rule must mention ABANDON + SendMessage so "
        "teammates know abandons travel through SendMessage with an "
        "ABANDON:-prefixed body, not silent exit (MAJOR-2)"
    )


def test_phase_5b_ci_failure_does_NOT_append_teammate_reply_rule():
    """Run-21 GAP-2 boundary: `phase_5b_ci_failure` is the one template
    in this module that is NOT a teammate-bound SendMessage body — it
    formats the abandonment-outcome `detail` string when CI fails. It
    must NOT carry the reply rule (the abandonment row would otherwise
    contain misleading "send your reply" prose in its detail field).
    """
    msg = phase_5b_ci_failure(wave_n=1, failed_checks=["tests"])
    assert TEAMMATE_REPLY_RULE.strip() not in msg
    assert msg == "CI failed after wave 1: ['tests']"


# ── GAP-7 — Phase 5d shutdown handshake ───────────────────────────────────


def test_phase_5d_shutdown_returns_valid_json_protocol():
    """GAP-7 (docs/kaizen/2026-05-24-bridge-smoke-3.md): the shutdown
    request body is a STRUCTURED JSON protocol message. Calling with an
    explicit request_id must round-trip exactly via json.loads; calling
    with no arg must default to a valid uuid4 string.
    """
    import json as _json
    import uuid as _uuid

    # Explicit request_id round-trips byte-exact through json.loads.
    out = phase_5d_shutdown("test-uuid-123")
    parsed = _json.loads(out)
    assert parsed == {"type": "shutdown_request", "request_id": "test-uuid-123"}

    # Default request_id is a valid uuid4 string (parses + matches the
    # version-4 nibble pattern). The protocol doesn't strictly require
    # uuid4, but the implementation contract does (so request_ids are
    # cryptographically unique across concurrent cycles).
    out_default = phase_5d_shutdown()
    parsed_default = _json.loads(out_default)
    assert parsed_default["type"] == "shutdown_request"
    rid = parsed_default["request_id"]
    assert isinstance(rid, str) and rid
    # Will raise ValueError if rid is not a valid uuid; .version checks v4.
    u = _uuid.UUID(rid)
    assert u.version == 4

    # And the rule that explicitly does NOT apply: TEAMMATE_REPLY_RULE
    # must NOT be appended — this is a protocol payload, not a prose
    # template.
    assert TEAMMATE_REPLY_RULE.strip() not in out
    assert TEAMMATE_REPLY_RULE.strip() not in out_default


def test_shutdown_rule_appended_to_teammate_spawn_prompt():
    """GAP-7: the SHUTDOWN_BEHAVIOR clause must ride in every teammate-bound
    dispatch via the existing TEAMMATE_REPLY_RULE append path, so every
    spawned teammate has the contract in-message.

    Asserts a sample teammate template (phase_1_agenda) carries the
    literal "shutdown_response" instruction substring after rendering.
    The other 9 teammate templates inherit the same clause via
    `+ TEAMMATE_REPLY_RULE` — the parametrised
    `test_every_template_appends_teammate_reply_rule` test above already
    proves every template ends with TEAMMATE_REPLY_RULE.
    """
    msg = phase_1_agenda(subject="x", cycle_n=1)
    assert "shutdown_response" in msg, (
        "phase_1_agenda's appended TEAMMATE_REPLY_RULE must include the "
        "GAP-7 SHUTDOWN_BEHAVIOR clause (literal 'shutdown_response' "
        "instruction) so spawned teammates know how to answer a "
        "shutdown_request protocol message."
    )
    # And the constant itself must carry the clause, so all 10 templates
    # propagate it without per-template wiring.
    assert "shutdown_response" in TEAMMATE_REPLY_RULE
    assert "shutdown_request" in TEAMMATE_REPLY_RULE


def test_phase_5d_shutdown_does_NOT_carry_reply_rule():
    """MINOR-1 (fix-loop iteration 2) regression test: mirror of
    `test_phase_5b_ci_failure_does_NOT_append_teammate_reply_rule`.

    `phase_5d_shutdown` is a STRUCTURED-JSON protocol body, not a
    teammate-readable prose template — appending TEAMMATE_REPLY_RULE
    would corrupt the JSON (the rule starts with a newline and contains
    free-form prose), making the message un-parseable on the teammate
    side. This test pins that property byte-exact.
    """
    out_explicit = phase_5d_shutdown("fixed-uuid-for-test")
    assert TEAMMATE_REPLY_RULE not in out_explicit
    assert TEAMMATE_REPLY_RULE.strip() not in out_explicit
    assert out_explicit == '{"type": "shutdown_request", "request_id": "fixed-uuid-for-test"}'

    # Default request_id form: still no reply rule appended.
    out_default = phase_5d_shutdown()
    assert TEAMMATE_REPLY_RULE not in out_default
    assert TEAMMATE_REPLY_RULE.strip() not in out_default
    # And the body is still valid JSON (no trailing whitespace, no rule).
    import json as _json

    parsed = _json.loads(out_default)
    assert set(parsed.keys()) == {"type", "request_id"}


# ── Group 2 (audit cleanup): brief templates aware of test side effects ──


def test_phase_5b_prime_fix_mentions_pytest_status():
    """F6: a fix that touches a tested contract must update those tests in
    the same change and report whether pytest passes locally."""
    msg = phase_5b_prime_fix(
        finding=Finding(
            finding_id="R1-1",
            reviewer="r",
            severity="blocker",
            finding="x",
            file_line="a.py:1",
        )
    )
    assert "tests in the same change" in msg
    assert "pytest" in msg


def test_phase_3_close_asks_for_test_files_in_reads():
    """F8: each touched file's corresponding test file must be in `reads`
    so the implementer can update tests in the same Phase 4 commit."""
    msg = phase_3_close(
        proposals=[{"agent": "be-1", "raw": "p"}],
        agreements=[{"agent": "be-1", "raw": "a"}],
    )
    assert "corresponding test file" in msg
    assert "tests/test_X.py" in msg


def test_phase_4_implementer_brief_directs_neighbor_file_reading():
    """F7: the implementer must list the parent directory and read any
    prefix/suffix neighbour file so the new change matches existing style."""
    item = {"id": "AI-1", "touches": ["migrations/003_x.sql"], "reads": []}
    msg = phase_4_implementer(item=item, wave_n=1)
    assert "list the directory" in msg
    assert "neighbor file" in msg
    assert "001_*.sql" in msg


def test_phase_4_implementer_reply_contract_includes_tests_status_tag():
    """F9: phase_4_implementer (and ONLY phase_4_implementer + phase_5b_prime_fix)
    appends a per-phase reply-format suffix demanding `OK:` / `BLOCKED:` plus a
    `tests: pass | fail | not-run` tag."""
    item = {"id": "AI-1", "touches": ["foo.py"], "reads": []}
    msg = phase_4_implementer(item=item, wave_n=1)
    assert "OK:" in msg
    assert "BLOCKED:" in msg
    assert "tests: pass | fail | not-run" in msg


def test_phase_5b_prime_fix_reply_contract_includes_tests_status_tag():
    """F9: phase_5b_prime_fix carries the same OK/BLOCKED + tests-status
    contract as phase_4_implementer."""
    msg = phase_5b_prime_fix(
        finding=Finding(
            finding_id="R1-1",
            reviewer="r",
            severity="blocker",
            finding="x",
            file_line="a.py:1",
        )
    )
    assert "OK:" in msg
    assert "BLOCKED:" in msg
    assert "tests: pass | fail | not-run" in msg


def test_global_TEAMMATE_REPLY_RULE_unchanged_by_F9_suffix():
    """F9: the per-phase suffix is appended in phase_4_implementer and
    phase_5b_prime_fix ONLY; the global TEAMMATE_REPLY_RULE itself must
    NOT carry the tests-status contract (other templates would otherwise
    inherit irrelevant reply rules)."""
    assert "tests: pass | fail | not-run" not in TEAMMATE_REPLY_RULE


def test_F9_suffix_not_appended_to_other_phase_templates():
    """F9 negative test: phase_1, phase_2, phase_3, phase_5b_prime_reviewer,
    and phase_5b_prime_pm_acceptance must NOT carry the tests-status suffix.
    Only phase_4_implementer and phase_5b_prime_fix do."""
    examples = [
        phase_1_agenda(subject="x", cycle_n=1),
        phase_2_preanalysis(agenda_items=["a"], participant="p"),
        phase_3_open(proposals=[{"agent": "a", "raw": "x"}]),
        phase_3_debate(),
        phase_3_close(
            proposals=[{"agent": "a", "raw": "x"}],
            agreements=[{"agent": "a", "raw": "y"}],
        ),
        phase_5b_prime_reviewer(iter_n=1, action_items=[{"id": "A"}], prior_findings=None),
        phase_5b_prime_pm_acceptance(
            findings=[
                Finding(
                    finding_id="R1-1",
                    reviewer="r",
                    severity="blocker",
                    finding="x",
                    file_line="a.py:1",
                )
            ],
            iter_n=1,
        ),
    ]
    for msg in examples:
        assert "tests: pass | fail | not-run" not in msg, (
            f"F9: per-phase suffix leaked into a non-fix template: {msg[-200:]!r}"
        )


def test_teammate_reply_rule_split_into_subconstants():
    """MINOR-2 (fix-loop iteration 2): TEAMMATE_REPLY_RULE is composed
    from `_REPLY_RULE + _SHUTDOWN_RULE`. Each sub-constant must contain
    its own contract verbatim, and the public constant must equal their
    concatenation. Byte-identity goldens reference the public constant
    so they auto-track.
    """
    from scripts.dispatch_templates import _REPLY_RULE, _SHUTDOWN_RULE

    # _REPLY_RULE carries the GAP-2 reply contract only.
    assert "Reply contract" in _REPLY_RULE
    assert 'to="team-lead"' in _REPLY_RULE
    assert "ABANDON" in _REPLY_RULE
    # _REPLY_RULE does NOT leak shutdown-contract prose.
    assert "shutdown_request" not in _REPLY_RULE
    assert "shutdown_response" not in _REPLY_RULE

    # _SHUTDOWN_RULE carries the GAP-7 shutdown contract only.
    assert "shutdown_request" in _SHUTDOWN_RULE
    assert "shutdown_response" in _SHUTDOWN_RULE
    # _SHUTDOWN_RULE does NOT redundantly include the GAP-2 reply prose.
    assert "Reply contract" not in _SHUTDOWN_RULE

    # The public constant is the concatenation.
    assert TEAMMATE_REPLY_RULE == _REPLY_RULE + _SHUTDOWN_RULE
