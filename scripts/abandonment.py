"""Abandonment handling — format report and record in DB.

When a cycle abandons mid-flight, this module:
  1. Renders a markdown report (design §4.5)
  2. Inserts an `abandonments` row keyed to the cycle

Capturing the report to Memex happens at the agent level via `memex:run`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from scripts.db import ABANDONMENT_JSON_COLUMNS, get_connection, row_to_dict_with_json

# ── Schema enum mirror — keep in sync with migrations/004 ─────────────────
# These frozensets mirror the CHECK constraints declared in
# migrations/004_phase_reached_review.sql. Co-locating them here (the closest
# Python neighbor to the DB layer) gives upstream call sites — notably
# scripts/run.py::orchestrate_run — a single import point so the orchestrator,
# the DB layer, and the SQL migration cannot drift independently again. Any
# change to the CHECK in migration 005+ MUST update these frozensets in the
# same commit.

VALID_PHASES: frozenset[str] = frozenset(
    {"agenda", "meeting", "implementation", "test", "review", "push"}
)
VALID_REASONS: frozenset[str] = frozenset(
    {
        "no_consensus",
        "destructive_rejected",
        "tests_unrecoverable",
        "review_unrecoverable",
        # F12 (audit cleanup): per-CI-category abandonment reasons. Mirrored
        # in migrations/005_reason_taxonomy.sql — any addition here MUST
        # also extend the SQL CHECK constraint in the next migration.
        "lint_failed",
        "security_failed",
        "sca_failed",
        # kaizen#93: dedicated bridge-timeout reason. Mirrored in
        # migrations/006_bridge_timeout_reason.sql — any addition here MUST
        # also extend the SQL CHECK constraint in the next migration.
        "bridge_timeout",
        "other",
    }
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_dict(row, cols) -> dict:
    """Thin wrapper around the shared db helper for abandonments rows.

    Preserved as a module-local name so existing call sites in this file
    continue to read naturally; the contract (JSON columns deserialised to
    Python list/dict) is enforced centrally in scripts/db.py.
    """
    return row_to_dict_with_json(row, cols, ABANDONMENT_JSON_COLUMNS)


def _slug_for(run_id: int, cycle_n: int) -> str:
    return f"kaizen:abandonment:{run_id}-cycle-{cycle_n}"


# ── Markdown rendering ─────────────────────────────────────────────────────


def format_report(
    project_name: str,
    git_url: str,
    run_id: int,
    cycle_n: int,
    subject: str | None,
    participants: list[str],
    phase_reached: str,
    reason: str,
    detail: str,
    artifacts: list[str],
    *,
    review_iteration_count: int | None = None,
    unresolved_findings: list[dict] | None = None,
    convergence_summary: str | None = None,
    reviewer_attribution: dict | None = None,
    recoverable_artifact: str | None = None,
    progress_classification: str | None = None,
    surviving_summary: str | None = None,
) -> str:
    """Render the abandonment report markdown (frontmatter + body).

    Format per design §4.5. `subject` may be None — rendered as "PM-directed".
    `participants` and `artifacts` are joined with ", " (artifacts also bulleted).

    The four review-loop fields (review_iteration_count, unresolved_findings,
    convergence_summary, reviewer_attribution) are optional. When ALL four
    are None the "Review-loop details" section is omitted (preserves the
    legacy report shape). Populate them only for `review_unrecoverable`
    abandonments — see scripts/abandonment.py::record_abandonment.

    kaizen#91: the three recoverable-artifact fields (recoverable_artifact,
    progress_classification, surviving_summary) are likewise optional. When ALL
    three are None the "Recoverable artifacts" section is omitted (legacy
    shape preserved). Populate them for bridge-timeout / wall-clock
    abandonments so the in-flight teammate work that survived the trip is
    visible and manually recoverable.
    """
    slug = _slug_for(run_id, cycle_n)
    title = f"Cycle {cycle_n} abandoned — {reason}"
    subject_display = subject or "PM-directed"
    date_str = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    participants_str = ", ".join(participants) if participants else "(none recorded)"
    artifacts_str = ", ".join(artifacts) if artifacts else "(none)"

    frontmatter = (
        "---\n"
        f"id: {slug}\n"
        f"title: {title}\n"
        "type: abandonment-report\n"
        f"project: {project_name}\n"
        "status: draft\n"
        "---\n"
    )

    body = (
        f"\nCycle: {cycle_n}\n"
        f"Date: {date_str}\n"
        f"Subject: {subject_display}\n"
        f"Participants: {participants_str}\n"
        f"Phase reached: {phase_reached}\n"
        f"Reason for abandonment: {reason}\n"
        f"Detail: {detail}\n"
        f"Artifacts: {artifacts_str}\n"
        f"\nRepo: {git_url}\n"
        f"Run id: {run_id}\n"
    )

    review_section = ""
    any_review_field = any(
        v is not None
        for v in (
            review_iteration_count,
            unresolved_findings,
            convergence_summary,
            reviewer_attribution,
        )
    )
    if any_review_field:
        iter_line = (
            f"Iterations run: {review_iteration_count}/5"
            if review_iteration_count is not None
            else "Iterations run: (unknown)"
        )
        conv_line = (
            f"Convergence summary: {convergence_summary}"
            if convergence_summary is not None
            else "Convergence summary: (none provided)"
        )

        if unresolved_findings:
            finding_lines = []
            for f in unresolved_findings:
                severity = f.get("severity", "?")
                reviewer = f.get("reviewer", "?")
                finding = f.get("finding", "?")
                file_line = f.get("file_line", "?")
                finding_lines.append(f"  - [{severity}] {reviewer}: {finding} ({file_line})")
            findings_block = "Unresolved findings:\n" + "\n".join(finding_lines)
        else:
            findings_block = "Unresolved findings: (none)"

        if reviewer_attribution:
            attrib_lines = [f"  - {fid}: {role}" for fid, role in reviewer_attribution.items()]
            attrib_block = "Reviewer attribution:\n" + "\n".join(attrib_lines)
        else:
            attrib_block = "Reviewer attribution: (none)"

        review_section = (
            "\n## Review-loop details (Phase 5b' only)\n"
            f"{iter_line}\n"
            f"{conv_line}\n"
            f"{findings_block}\n"
            f"{attrib_block}\n"
        )

    # kaizen#91 — recoverable-artifact section. Gated on any field present so
    # legacy abandonments (all three None) keep their byte-for-byte shape.
    recoverable_section = ""
    any_recoverable_field = any(
        v is not None for v in (recoverable_artifact, progress_classification, surviving_summary)
    )
    if any_recoverable_field:
        branch_line = (
            f"Recoverable branch: {recoverable_artifact}"
            if recoverable_artifact is not None
            else "Recoverable branch: (none)"
        )
        class_line = (
            f"Progress: {progress_classification}"
            if progress_classification is not None
            else "Progress: (unknown)"
        )
        surviving_line = (
            f"Surviving work: {surviving_summary}"
            if surviving_summary is not None
            else "Surviving work: (none)"
        )
        note = (
            "Recovery is MANUAL and out of automated scope (kaizen#91): the "
            "teammate work that completed before the trip survives on the "
            "feature branch in the experiment clone for a human to inspect or "
            "hand-finish. No work was auto-resumed, committed, or pushed."
        )
        recoverable_section = (
            f"\n## Recoverable artifacts\n{branch_line}\n{class_line}\n{surviving_line}\n{note}\n"
        )

    return frontmatter + body + review_section + recoverable_section


# ── DB write ───────────────────────────────────────────────────────────────


def record_abandonment(
    db_path: str,
    cycle_id: int,
    phase_reached: str,
    reason: str,
    detail: str,
    report_memex_slug: str | None,
    *,
    review_iteration_count: int | None = None,
    unresolved_findings: list[dict] | None = None,
    convergence_summary: str | None = None,
    reviewer_attribution: dict | None = None,
) -> dict:
    """Insert an abandonments row. Returns the row.

    The four review-loop kwargs are optional and default to None for
    backwards-compatibility with pre-Phase-5b' call sites. They are
    intended for `reason='review_unrecoverable'` abandonments produced by
    the Phase 5b' independent-reviewer fix-loop:

      review_iteration_count: int — how many fix-loop iterations ran (max 5)
      unresolved_findings: list[dict] — final unresolved issues, each
        {reviewer, severity, finding, file_line}
      convergence_summary: str — why the fix loop couldn't converge
      reviewer_attribution: dict — {finding_id: reviewer_role_id} mapping

    `unresolved_findings` and `reviewer_attribution` are JSON-serialised to
    TEXT in the DB; the returned row dict has them deserialised back to
    Python list/dict. NULL JSON columns come back as None.
    """
    now = _now()
    unresolved_json = json.dumps(unresolved_findings) if unresolved_findings is not None else None
    attrib_json = json.dumps(reviewer_attribution) if reviewer_attribution is not None else None
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO abandonments "
            "(cycle_id, phase_reached, reason, detail, report_memex_slug, created_at, "
            " review_iteration_count, unresolved_findings, convergence_summary, "
            " reviewer_attribution) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cycle_id,
                phase_reached,
                reason,
                detail,
                report_memex_slug,
                now,
                review_iteration_count,
                unresolved_json,
                convergence_summary,
                attrib_json,
            ),
        )
        conn.commit()
        new_id = cur.lastrowid
        cur = conn.execute("SELECT * FROM abandonments WHERE id = ?", (new_id,))
        row = cur.fetchone()
        cols = [c[0] for c in cur.description]
    finally:
        conn.close()
    return _row_to_dict(row, cols)


# ── End-to-end orchestrator ────────────────────────────────────────────────


def process_abandonment(
    db_path: str,
    project: dict,
    run_id: int,
    cycle_id: int,
    cycle_n: int,
    subject: str | None,
    participants: list[str],
    phase_reached: str,
    reason: str,
    detail: str,
    artifacts: list[str],
    *,
    review_iteration_count: int | None = None,
    unresolved_findings: list[dict] | None = None,
    convergence_summary: str | None = None,
    reviewer_attribution: dict | None = None,
    recoverable_artifact: str | None = None,
    progress_classification: str | None = None,
    surviving_summary: str | None = None,
) -> tuple[dict, str]:
    """Format report → record abandonment row → return (row, rendered markdown).

    Returns a 2-tuple of (abandonments row dict, rendered markdown string).
    The markdown is returned so callers can capture it to Memex via memex:run.

    See `record_abandonment` for the four optional review-loop kwargs — they
    are threaded through both the markdown renderer and the DB insert.

    kaizen#91: the three recoverable-artifact kwargs are threaded into the
    markdown renderer ONLY (they are surfaced in the report + the row's
    `detail`, not a dedicated DB column — no migration). They default to None
    so every legacy call site is unaffected.
    """
    markdown = format_report(
        project_name=project["name"],
        git_url=project["git_url"],
        run_id=run_id,
        cycle_n=cycle_n,
        subject=subject,
        participants=participants,
        phase_reached=phase_reached,
        reason=reason,
        detail=detail,
        artifacts=artifacts,
        review_iteration_count=review_iteration_count,
        unresolved_findings=unresolved_findings,
        convergence_summary=convergence_summary,
        reviewer_attribution=reviewer_attribution,
        recoverable_artifact=recoverable_artifact,
        progress_classification=progress_classification,
        surviving_summary=surviving_summary,
    )
    slug = _slug_for(run_id, cycle_n)
    row = record_abandonment(
        db_path=db_path,
        cycle_id=cycle_id,
        phase_reached=phase_reached,
        reason=reason,
        detail=detail,
        report_memex_slug=slug,
        review_iteration_count=review_iteration_count,
        unresolved_findings=unresolved_findings,
        convergence_summary=convergence_summary,
        reviewer_attribution=reviewer_attribution,
    )
    return row, markdown
