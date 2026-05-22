"""Abandonment handling — format report and record in DB.

When a cycle abandons mid-flight, this module:
  1. Renders a markdown report (design §4.5)
  2. Inserts an `abandonments` row keyed to the cycle

Capturing the report to Memex happens at the agent level via `memex:run`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from scripts.db import get_connection


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_dict(row, cols) -> dict:
    return dict(zip(cols, row, strict=False))


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
) -> str:
    """Render the abandonment report markdown (frontmatter + body).

    Format per design §4.5. `subject` may be None — rendered as "PM-directed".
    `participants` and `artifacts` are joined with ", " (artifacts also bulleted).
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
    return frontmatter + body


# ── DB write ───────────────────────────────────────────────────────────────


def record_abandonment(
    db_path: str,
    cycle_id: int,
    phase_reached: str,
    reason: str,
    detail: str,
    report_memex_slug: str | None,
) -> dict:
    """Insert an abandonments row. Returns the row."""
    now = _now()
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO abandonments "
            "(cycle_id, phase_reached, reason, detail, report_memex_slug, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (cycle_id, phase_reached, reason, detail, report_memex_slug, now),
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
) -> tuple[dict, str]:
    """Format report → record abandonment row → return (row, rendered markdown).

    Returns a 2-tuple of (abandonments row dict, rendered markdown string).
    The markdown is returned so callers can capture it to Memex via memex:run.
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
    )
    slug = _slug_for(run_id, cycle_n)
    row = record_abandonment(
        db_path=db_path,
        cycle_id=cycle_id,
        phase_reached=phase_reached,
        reason=reason,
        detail=detail,
        report_memex_slug=slug,
    )
    return row, markdown
