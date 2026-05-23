"""PR open — render bundled PR body and invoke `gh pr create`.

Wave 5 of the implementation plan. This module:
  1. Loads a run + its project + all cycles + all abandonments from the
     kaizen DB.
  2. Renders the PR title and body per design §4.6.
  3. Invokes `gh pr create` from inside the run's clone directory.
  4. Writes the returned PR URL back to `runs.pr_url`.

# DESIGN NOTE — clone directory
The `runs` table does NOT store the experiment clone path. The orchestrator
(`scripts.run.orchestrate_run`) knows the path and passes it through. This
keeps the schema small and avoids tracking ephemeral filesystem state in
the DB. Cleanup of the clone is the orchestrator's job
(`scripts.run.cleanup_after_pr`) and happens AFTER this module returns.

# DESIGN NOTE — all-abandoned case
When `cycles_succeeded == 0`, the PR is still opened. The body simply has
no commit shas — only the cycle outcomes and the abandonment reports.
This matches design §3.3 "All cycles abandoned" — user can review the
reports without losing the work.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from scripts.db import get_connection

# ── DB loaders ─────────────────────────────────────────────────────────────


def _row_to_dict(row, cols) -> dict:
    return dict(zip(cols, row, strict=False))


def load_run_context(db_path: str, run_id: int) -> tuple[dict, dict, list[dict], list[dict]]:
    """Load run + project + ordered cycles + ordered abandonments.

    Returns (run, project, cycles, abandonments). Cycles are ordered by
    cycle_n; abandonments by cycle_id (so the join order matches cycle_n).

    Raises RuntimeError if the run or its project cannot be found.
    """
    conn = get_connection(db_path)
    try:
        cur = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
        row = cur.fetchone()
        if row is None:
            raise RuntimeError(f"No run found for id={run_id}")
        cols = [c[0] for c in cur.description]
        run = _row_to_dict(row, cols)

        cur = conn.execute("SELECT * FROM projects WHERE id = ?", (run["project_id"],))
        row = cur.fetchone()
        if row is None:
            raise RuntimeError(
                f"No project found for run {run_id} (project_id={run['project_id']})"
            )
        cols = [c[0] for c in cur.description]
        project = _row_to_dict(row, cols)

        cur = conn.execute(
            "SELECT * FROM cycles WHERE run_id = ? ORDER BY cycle_n",
            (run_id,),
        )
        rows = cur.fetchall()
        cols = [c[0] for c in cur.description]
        cycles = [_row_to_dict(r, cols) for r in rows]

        cycle_ids = [c["id"] for c in cycles]
        if cycle_ids:
            placeholders = ",".join("?" for _ in cycle_ids)
            # nosec B608 — `placeholders` is built only from literal "?" chars
            # (count of cycle_ids); all values flow through parameter binding.
            cur = conn.execute(
                f"SELECT * FROM abandonments WHERE cycle_id IN ({placeholders}) ORDER BY cycle_id",  # nosec B608
                cycle_ids,
            )
            rows = cur.fetchall()
            cols = [c[0] for c in cur.description]
            abandonments = [_row_to_dict(r, cols) for r in rows]
        else:
            abandonments = []
    finally:
        conn.close()
    return run, project, cycles, abandonments


# ── Rendering ──────────────────────────────────────────────────────────────

_DETAIL_TRUNCATE = 200


def _fmt_ts(ts: str | None) -> str:
    """Format an ISO timestamp string as 'YYYY-MM-DD HH:MM UTC'.

    Normalises to UTC before formatting. Tolerates None and non-ISO inputs
    (returns "—" / the raw value).
    """
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return ts
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _subject_or_pm(subject: str | None) -> str:
    return subject if subject else "PM-directed"


def render_pr_body(
    run: dict,
    project: dict,
    cycles: list[dict],
    abandonments: list[dict],
) -> tuple[str, str]:
    """Render (title, body) for the bundled PR per design §4.6."""
    n = run["cycles_requested"]
    s = sum(1 for c in cycles if c["status"] == "success")
    a = sum(1 for c in cycles if c["status"] == "abandoned")
    subject = _subject_or_pm(run.get("subject"))
    title = f"kaizen: {subject} — {n} cycles, {s} succeeded / {a} abandoned"

    # Index abandonments by cycle_id for quick lookup in cycle sections.
    ab_by_cycle_id = {ab["cycle_id"]: ab for ab in abandonments}

    parts: list[str] = []
    parts.append("## Summary\n")
    parts.append("Multi-cycle improvement run against this repo.\n")
    parts.append("| | |")
    parts.append("|---|---|")
    parts.append(f"| Cycles requested | {n} |")
    parts.append(f"| Succeeded | {s} |")
    parts.append(f"| Abandoned | {a} |")
    parts.append(f"| Run started | {_fmt_ts(run.get('started_at'))} |")
    parts.append(f"| Run ended | {_fmt_ts(run.get('ended_at'))} |")
    parts.append("")
    parts.append("## Cycles")
    parts.append("")

    for cycle in cycles:
        status = cycle["status"]
        header_label = "success" if status == "success" else "abandoned"
        parts.append(f"### Cycle {cycle['cycle_n']} — {header_label}")
        parts.append(f"- Subject: {_subject_or_pm(cycle.get('subject'))}")
        if status == "success":
            sha = cycle.get("commit_sha") or ""
            short = sha[:7] if sha else "—"
            parts.append(f"- Commit: `{short}`")
            minutes_slug = cycle.get("minutes_memex_slug")
            if minutes_slug:
                parts.append(f"- Minutes (Kaizen wiki): `{minutes_slug}`")
        else:
            parts.append("- Commit: —")
            ab = ab_by_cycle_id.get(cycle["id"])
            if ab is not None:
                parts.append(f"- Phase reached: {ab['phase_reached']}")
                parts.append(f"- Reason: {ab['reason']}")
                if ab.get("report_memex_slug"):
                    parts.append(f"- Report: `{ab['report_memex_slug']}`")
                detail = ab.get("detail") or ""
                if len(detail) > _DETAIL_TRUNCATE:
                    detail_short = detail[:_DETAIL_TRUNCATE] + "..."
                else:
                    detail_short = detail
                parts.append(f"- Detail summary: {detail_short}")
        parts.append("")

    if abandonments:
        parts.append("## Abandonment reports")
        parts.append("")
        parts.append("See Kaizen memex entries:")
        for ab in abandonments:
            slug = ab.get("report_memex_slug")
            if slug:
                parts.append(f"- `{slug}`")
        parts.append("")

    parts.append(
        f"\U0001f916 Generated by Kaizen against {project['name']} at {project['git_url']}"
    )

    body = "\n".join(parts) + "\n"
    return title, body


# ── gh invocation ──────────────────────────────────────────────────────────


def open_pr(
    clone_dir: Path,
    title: str,
    body: str,
    base_branch: str,
    head_branch: str,
) -> str:
    """Invoke `gh pr create` from inside clone_dir. Returns the PR URL.

    Raises RuntimeError on non-zero exit, including gh's stderr in the
    message.
    """
    cmd = [
        "gh",
        "pr",
        "create",
        "--title",
        title,
        "--body",
        body,
        "--base",
        base_branch,
        "--head",
        head_branch,
    ]
    result = subprocess.run(
        cmd,
        cwd=str(clone_dir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"gh pr create failed (exit {result.returncode}): {(result.stderr or '').strip()}"
        )
    # gh prints the PR URL as the last non-empty line of stdout.
    lines = [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]
    if not lines:
        raise RuntimeError("gh pr create returned exit 0 but no URL on stdout")
    return lines[-1]


# ── DB update ──────────────────────────────────────────────────────────────


def update_run_pr_url(db_path: str, run_id: int, pr_url: str) -> None:
    """Persist pr_url onto the runs row."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE runs SET pr_url = ? WHERE id = ?",
            (pr_url, run_id),
        )
        conn.commit()
    finally:
        conn.close()


# ── Orchestrator ───────────────────────────────────────────────────────────


def open_pr_for_run(db_path: str, run_id: int, clone_dir: Path) -> str:
    """Full PR-open flow: load → render → invoke gh → persist URL.

    Returns the PR URL.
    """
    run, project, cycles, abandonments = load_run_context(db_path, run_id)
    title, body = render_pr_body(run, project, cycles, abandonments)
    pr_url = open_pr(
        clone_dir=clone_dir,
        title=title,
        body=body,
        base_branch=project["base_branch"],
        head_branch=run["branch"],
    )
    update_run_pr_url(db_path, run_id, pr_url)
    return pr_url


# ── CLI ────────────────────────────────────────────────────────────────────


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: pr.py <run_id> <clone_dir>", file=sys.stderr)
        return 1
    try:
        run_id = int(argv[0])
    except ValueError:
        print(f"run_id must be an integer; got {argv[0]!r}", file=sys.stderr)
        return 1
    clone_dir = Path(argv[1])
    db_path = ".ai/memex.db"
    try:
        pr_url = open_pr_for_run(db_path, run_id, clone_dir)
    except Exception as exc:
        print(f"PR open failed: {exc}", file=sys.stderr)
        return 1
    print(pr_url)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
