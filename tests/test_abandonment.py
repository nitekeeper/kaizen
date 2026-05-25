"""Tests for scripts/abandonment.py — report format + memex + DB."""

from __future__ import annotations

import sqlite3

import pytest

from scripts.abandonment import (
    VALID_REASONS,
    format_report,
    process_abandonment,
    record_abandonment,
)
from scripts.cycle import record_cycle_abandoned
from scripts.migrate import MIGRATIONS_DIR, apply_migrations
from scripts.project import create_project
from scripts.run import create_run


@pytest.fixture
def db(tmp_path) -> str:
    db_path = str(tmp_path / "kaizen.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    return db_path


@pytest.fixture
def run_and_cycle(db) -> dict:
    project = create_project(
        db,
        git_url="https://github.com/owner/repo.git",
        name="repo",
        base_branch="main",
        test_command="pytest",
        read_paths=[],
        expert_roster=[],
        language="python",
    )
    run = create_run(
        db,
        project_id=project["id"],
        branch="kaizen/x-2026-05-16-1200",
        cycles_requested=1,
        subject="x",
    )
    cycle = record_cycle_abandoned(
        db,
        run_id=run["id"],
        cycle_n=1,
        subject="x",
        started_at="2026-05-16T12:00:00+00:00",
    )
    return {"project": project, "run": run, "cycle": cycle}


# ── format_report ──────────────────────────────────────────────────────────


def test_format_report_includes_all_required_fields():
    md = format_report(
        project_name="owner-repo",
        git_url="https://github.com/owner/repo.git",
        run_id=7,
        cycle_n=3,
        subject="improve error messages",
        participants=["pm", "backend-engineer-1", "ai-safety-researcher"],
        phase_reached="meeting",
        reason="no_consensus",
        detail="Agents could not agree on a unified error format.",
        artifacts=["kaizen:cycle:7-3-draft", "kaizen:cycle:7-3-logs"],
    )
    # Frontmatter
    assert "---\n" in md
    assert "id: kaizen:abandonment:7-cycle-3" in md
    assert "title: Cycle 3 abandoned — no_consensus" in md
    assert "type: abandonment-report" in md
    assert "project: owner-repo" in md
    assert "status: draft" in md
    # Body — every required field
    assert "Cycle: 3" in md
    assert "Date:" in md and "UTC" in md
    assert "Subject: improve error messages" in md
    assert "Participants: pm, backend-engineer-1, ai-safety-researcher" in md
    assert "Phase reached: meeting" in md
    assert "Reason for abandonment: no_consensus" in md
    assert "Detail: Agents could not agree on a unified error format." in md
    assert "Artifacts:" in md
    assert "kaizen:cycle:7-3-draft" in md


def test_format_report_handles_missing_subject_and_empty_lists():
    md = format_report(
        project_name="o-r",
        git_url="u",
        run_id=1,
        cycle_n=1,
        subject=None,
        participants=[],
        phase_reached="agenda",
        reason="other",
        detail="d",
        artifacts=[],
    )
    assert "Subject: PM-directed" in md
    assert "Participants: (none recorded)" in md
    assert "Artifacts: (none)" in md


# ── record_abandonment ────────────────────────────────────────────────────


def test_record_abandonment_inserts_row(db, run_and_cycle):
    row = record_abandonment(
        db_path=db,
        cycle_id=run_and_cycle["cycle"]["id"],
        phase_reached="meeting",
        reason="no_consensus",
        detail="agents could not agree",
        report_memex_slug="kaizen:abandonment:1-cycle-1",
    )
    assert row["cycle_id"] == run_and_cycle["cycle"]["id"]
    assert row["phase_reached"] == "meeting"
    assert row["reason"] == "no_consensus"
    assert row["detail"] == "agents could not agree"
    assert row["report_memex_slug"] == "kaizen:abandonment:1-cycle-1"
    assert row["created_at"]


# ── new reason: review_unrecoverable ──────────────────────────────────────


def test_record_abandonment_accepts_review_unrecoverable(db, run_and_cycle):
    """Confirm the new reason `review_unrecoverable` is accepted by the schema."""
    row = record_abandonment(
        db_path=db,
        cycle_id=run_and_cycle["cycle"]["id"],
        phase_reached="review",
        reason="review_unrecoverable",
        detail="Phase 5b' fix loop exhausted at max 5 iterations",
        report_memex_slug="kaizen:abandonment:test-cycle-1",
    )
    assert row["reason"] == "review_unrecoverable"
    assert row["phase_reached"] == "review"


# ── Phase 5b' substrate — structured review-loop fields ──────────────────


def test_record_abandonment_with_review_fields(db, run_and_cycle):
    """All 4 review-loop kwargs round-trip through JSON serialisation."""
    findings = [
        {
            "reviewer": "security-engineer-1",
            "severity": "blocker",
            "finding": "SQL injection in build_query",
            "file_line": "scripts/db.py:42",
        },
        {
            "reviewer": "prompt-engineer-1",
            "severity": "major",
            "finding": "Embedded SQL should be parameterised at the caller",
            "file_line": "scripts/db.py:42",
        },
    ]
    attribution = {
        "f-001": "security-engineer-1",
        "f-002": "prompt-engineer-1",
    }
    row = record_abandonment(
        db_path=db,
        cycle_id=run_and_cycle["cycle"]["id"],
        phase_reached="review",
        reason="review_unrecoverable",
        detail="fix loop exhausted",
        report_memex_slug="kaizen:abandonment:1-cycle-1",
        review_iteration_count=5,
        unresolved_findings=findings,
        convergence_summary=(
            "f-001 was re-flagged in rounds 2/3/4; the implementer's "
            "parameterisation attempt did not satisfy security-engineer-1."
        ),
        reviewer_attribution=attribution,
    )
    # Scalar fields
    assert row["review_iteration_count"] == 5
    assert row["convergence_summary"].startswith("f-001 was re-flagged")
    # JSON columns must come back as Python structures, NOT strings.
    assert isinstance(row["unresolved_findings"], list)
    assert row["unresolved_findings"] == findings
    assert isinstance(row["reviewer_attribution"], dict)
    assert row["reviewer_attribution"] == attribution


def test_record_abandonment_review_phase_check_constraint_allows_review(db, run_and_cycle):
    """phase_reached='review' must not raise after migration 004."""
    row = record_abandonment(
        db_path=db,
        cycle_id=run_and_cycle["cycle"]["id"],
        phase_reached="review",
        reason="review_unrecoverable",
        detail="d",
        report_memex_slug=None,
    )
    assert row["phase_reached"] == "review"


def test_record_abandonment_push_phase_check_constraint_allows_push(db, run_and_cycle):
    """phase_reached='push' must not raise after migration 004."""
    row = record_abandonment(
        db_path=db,
        cycle_id=run_and_cycle["cycle"]["id"],
        phase_reached="push",
        reason="other",
        detail="git push failed: refs not advertised",
        report_memex_slug=None,
    )
    assert row["phase_reached"] == "push"


def test_record_abandonment_backwards_compatible_without_review_fields(db, run_and_cycle):
    """Existing call sites that omit the 4 new kwargs still work."""
    row = record_abandonment(
        db_path=db,
        cycle_id=run_and_cycle["cycle"]["id"],
        phase_reached="meeting",
        reason="no_consensus",
        detail="agents could not agree",
        report_memex_slug="kaizen:abandonment:1-cycle-1",
    )
    # All 4 new columns must come back as None when not provided.
    assert row["review_iteration_count"] is None
    assert row["unresolved_findings"] is None
    assert row["convergence_summary"] is None
    assert row["reviewer_attribution"] is None


# ── CHECK constraint — contract-pinning regression guard ─────────────────


def test_record_abandonment_rejects_unknown_phase_reached(db, run_and_cycle):
    """Contract-pinning test: phase_reached='unknown' MUST be rejected by the
    schema CHECK constraint (migration 004 permits only agenda|meeting|
    implementation|test|review|push).

    The orchestrator used to default to "unknown" when the cycle outcome was
    malformed — that default would crash here with sqlite3.IntegrityError
    *after* the cycle's work was done. The defensive ValueError raise added
    to scripts/run.py now prevents that, but this test pins the underlying
    CHECK contract so any future loosening of the constraint (e.g. someone
    adding "unknown" to the enum) fails this test and forces a deliberate
    decision rather than silent reintroduction of the bug class.
    """
    with pytest.raises(sqlite3.IntegrityError) as exc_info:
        record_abandonment(
            db_path=db,
            cycle_id=run_and_cycle["cycle"]["id"],
            phase_reached="unknown",
            reason="other",
            detail="this insert must be rejected by the CHECK",
            report_memex_slug=None,
        )
    # Pin the exact SQLite wording so a NOT NULL / FK violation cannot
    # falsely satisfy this test — SQLite 3.x has been stable on this
    # exact string for over a decade.
    assert "CHECK constraint failed" in str(exc_info.value)


def test_format_report_includes_review_section_when_fields_present():
    findings = [
        {
            "reviewer": "security-engineer-1",
            "severity": "blocker",
            "finding": "SQL injection",
            "file_line": "scripts/db.py:42",
        },
    ]
    attribution = {"f-001": "security-engineer-1"}
    md = format_report(
        project_name="owner-repo",
        git_url="https://github.com/owner/repo.git",
        run_id=7,
        cycle_n=3,
        subject="harden DB layer",
        participants=["pm", "security-engineer-1"],
        phase_reached="review",
        reason="review_unrecoverable",
        detail="fix loop exhausted",
        artifacts=[],
        review_iteration_count=5,
        unresolved_findings=findings,
        convergence_summary="implementer could not parameterise the query",
        reviewer_attribution=attribution,
    )
    assert "## Review-loop details (Phase 5b' only)" in md
    assert "Iterations run: 5/5" in md
    assert "Convergence summary: implementer could not parameterise the query" in md
    assert "Unresolved findings:" in md
    assert "[blocker] security-engineer-1: SQL injection (scripts/db.py:42)" in md
    assert "Reviewer attribution:" in md
    assert "f-001: security-engineer-1" in md


def test_format_report_review_section_snapshot_pins_exact_rendering():
    """Pin the exact markdown for the Review-loop section against a golden string.

    Catches accidental whitespace / ordering / punctuation drift in the
    Phase 5b' renderer. The frontmatter & body above this section are
    covered by other tests; we slice from the section header onward.
    """
    findings = [
        {
            "reviewer": "security-engineer-1",
            "severity": "blocker",
            "finding": "SQL injection in build_query",
            "file_line": "scripts/db.py:42",
        },
        {
            "reviewer": "prompt-engineer-1",
            "severity": "major",
            "finding": "Embedded SQL should be parameterised at caller",
            "file_line": "scripts/db.py:42",
        },
    ]
    attribution = {
        "f-001": "security-engineer-1",
        "f-002": "prompt-engineer-1",
    }
    md = format_report(
        project_name="owner-repo",
        git_url="https://github.com/owner/repo.git",
        run_id=7,
        cycle_n=3,
        subject="harden DB layer",
        participants=["pm", "security-engineer-1"],
        phase_reached="review",
        reason="review_unrecoverable",
        detail="fix loop exhausted",
        artifacts=[],
        review_iteration_count=5,
        unresolved_findings=findings,
        convergence_summary="implementer could not parameterise the query",
        reviewer_attribution=attribution,
    )
    expected = (
        "\n## Review-loop details (Phase 5b' only)\n"
        "Iterations run: 5/5\n"
        "Convergence summary: implementer could not parameterise the query\n"
        "Unresolved findings:\n"
        "  - [blocker] security-engineer-1: SQL injection in build_query (scripts/db.py:42)\n"
        "  - [major] prompt-engineer-1: Embedded SQL should be parameterised at caller (scripts/db.py:42)\n"
        "Reviewer attribution:\n"
        "  - f-001: security-engineer-1\n"
        "  - f-002: prompt-engineer-1\n"
    )
    # Locate the review section start and compare from there to end-of-string.
    idx = md.index("\n## Review-loop details")
    assert md[idx:] == expected, (
        f"Review-loop section drift.\n--- expected ---\n{expected!r}\n--- actual ---\n{md[idx:]!r}"
    )


def test_format_report_tolerates_findings_with_missing_keys():
    """The renderer must not crash on a finding dict missing inner keys.

    Contract: `unresolved_findings` SHOULD be `{reviewer, severity, finding,
    file_line}` per scripts/abandonment.py docstring. The renderer enforces
    this leniently — any missing key renders as '?' rather than raising
    KeyError. This test pins that behaviour so a stricter rewrite (e.g.
    `f["severity"]`) doesn't silently regress.
    """
    findings = [
        {"severity": "blocker"},  # only severity
        {},  # all keys missing
        {
            "reviewer": "security-engineer-1",
            "severity": "major",
            "finding": "issue",
            "file_line": "f.py:1",
        },  # well-formed
    ]
    md = format_report(
        project_name="owner-repo",
        git_url="https://github.com/owner/repo.git",
        run_id=7,
        cycle_n=3,
        subject="x",
        participants=["pm"],
        phase_reached="review",
        reason="review_unrecoverable",
        detail="d",
        artifacts=[],
        review_iteration_count=1,
        unresolved_findings=findings,
        convergence_summary="c",
        reviewer_attribution=None,
    )
    # Lenient placeholder for missing fields.
    assert "[blocker] ?: ? (?)" in md  # severity-only finding
    assert "[?] ?: ? (?)" in md  # all-missing finding
    # Well-formed finding still renders normally.
    assert "[major] security-engineer-1: issue (f.py:1)" in md


def test_format_report_omits_review_section_when_fields_absent():
    md = format_report(
        project_name="owner-repo",
        git_url="https://github.com/owner/repo.git",
        run_id=7,
        cycle_n=3,
        subject="x",
        participants=["pm"],
        phase_reached="meeting",
        reason="no_consensus",
        detail="d",
        artifacts=[],
    )
    assert "Review-loop details" not in md


# ── process_abandonment full flow ──────────────────────────────────────────


def test_process_abandonment_full_flow(db, run_and_cycle):
    row, markdown = process_abandonment(
        db_path=db,
        project=run_and_cycle["project"],
        run_id=run_and_cycle["run"]["id"],
        cycle_id=run_and_cycle["cycle"]["id"],
        cycle_n=1,
        subject="x",
        participants=["pm"],
        phase_reached="meeting",
        reason="no_consensus",
        detail="d",
        artifacts=[],
    )
    expected_slug = f"kaizen:abandonment:{run_and_cycle['run']['id']}-cycle-1"
    assert row["report_memex_slug"] == expected_slug
    assert row["reason"] == "no_consensus"
    assert row["cycle_id"] == run_and_cycle["cycle"]["id"]
    assert "Cycle 1 abandoned" in markdown
    assert "type: abandonment-report" in markdown


# ── F12 — extended reason taxonomy ────────────────────────────────────────


def test_valid_reasons_includes_new_taxonomy():
    """F12 (audit cleanup): the per-CI-category reasons must be in
    VALID_REASONS so the orchestrator's allowlist guard accepts them."""
    assert "lint_failed" in VALID_REASONS
    assert "security_failed" in VALID_REASONS
    assert "sca_failed" in VALID_REASONS
    # Existing reasons are preserved.
    assert "tests_unrecoverable" in VALID_REASONS
    assert "review_unrecoverable" in VALID_REASONS


@pytest.mark.parametrize("new_reason", ["lint_failed", "security_failed", "sca_failed"])
def test_record_abandonment_accepts_new_reasons(db, run_and_cycle, new_reason):
    """F12 (audit cleanup): migration 005 extended the CHECK constraint to
    accept the three new categories — inserting a row with any of them must
    succeed (not raise an IntegrityError)."""
    row = record_abandonment(
        db_path=db,
        cycle_id=run_and_cycle["cycle"]["id"],
        phase_reached="test",
        reason=new_reason,
        detail="auto",
        report_memex_slug=None,
    )
    assert row["reason"] == new_reason
