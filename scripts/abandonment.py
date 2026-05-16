"""Abandonment handling — format report, capture to memex, record in DB.

When a cycle abandons mid-flight, this module:
  1. Renders a markdown report (design §4.5)
  2. Captures it to Kaizen's own memex via `memex capture` (best-effort)
  3. Inserts an `abandonments` row keyed to the cycle

`memex capture` is best-effort: if memex is not on PATH, or the subprocess
fails, the slug is still returned and the abandonment is still recorded.
The report can be re-ingested later by the user.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import datetime, timezone

from scripts.db import get_connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row, cols) -> dict:
    return dict(zip(cols, row))


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
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
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


# ── Memex capture (best-effort) ────────────────────────────────────────────

def capture_to_memex(slug: str, markdown_content: str) -> str:
    """Capture markdown to Kaizen's memex via `memex capture`. Returns slug.

    Best-effort: if `memex` is not on PATH or the subprocess fails, emits a
    warning to stderr and returns the slug anyway. Abandonment recording
    must not be blocked on memex availability.
    """
    if shutil.which("memex") is None:
        print(
            f"warning: `memex` not on PATH; skipped capture of {slug} "
            "(report can be re-ingested later)",
            file=sys.stderr,
        )
        return slug

    try:
        result = subprocess.run(
            ["memex", "capture", "--id", slug],
            input=markdown_content,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode != 0:
            print(
                f"warning: `memex capture` exited {result.returncode} for {slug}; "
                f"stderr: {result.stderr.strip()}",
                file=sys.stderr,
            )
    except OSError as exc:
        print(
            f"warning: failed to invoke `memex` for {slug}: {exc}",
            file=sys.stderr,
        )
    return slug


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
) -> dict:
    """Format report → capture to memex → record abandonment row.

    Returns the inserted abandonments row.
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
    capture_to_memex(slug, markdown)
    return record_abandonment(
        db_path=db_path,
        cycle_id=cycle_id,
        phase_reached=phase_reached,
        reason=reason,
        detail=detail,
        report_memex_slug=slug,
    )
