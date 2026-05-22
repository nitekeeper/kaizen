"""Run CRUD + multi-cycle orchestrator.

`orchestrate_run` is the top-level entry point used by the slash command
(Wave 7). It:
  1. Looks up the project by git URL (raises if unregistered)
  2. Clones the target into `<kaizen>/experiment/<owner>-<repo>/`
  3. Seeds atelier's schema + roles + wiki dir in the clone
  4. Creates the kaizen run branch
  5. Inserts a `runs` row (status='running')
  6. Loops N cycles, calling the injected `cycle_executor` (or the stub).
     On success: records a cycles row.
     On abandonment: records a cycles row + abandonments row, continues.
  7. Pushes the branch. If push fails, leaves the clone in place and
     returns status='failed' — caller can recover manually.
  8. Finalizes the run (status='complete', cycle counts).
  9. Returns the state Wave 5's PR-open step needs.

# DESIGN NOTE — clone cleanup
The design (§3.2) places cleanup AFTER PR open. Wave 4 only handles up to
push + finalize; it does NOT delete the clone. The caller (Wave 5 / the
SKILL prose) calls `cleanup_after_pr(experiment_dir)` once the PR is open.
This lets a test or recovery flow inspect the clone before teardown.
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from scripts.db import get_connection


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_dict(row, cols) -> dict:
    return dict(zip(cols, row, strict=False))


# ── Run CRUD ───────────────────────────────────────────────────────────────


def create_run(
    db_path: str,
    project_id: int,
    branch: str,
    cycles_requested: int,
    subject: str | None,
) -> dict:
    """Insert a runs row with status='running'. Returns the inserted row."""
    started_at = _now()
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO runs "
            "(project_id, branch, pr_url, cycles_requested, cycles_succeeded, "
            " cycles_abandoned, subject, started_at, ended_at, status) "
            "VALUES (?, ?, NULL, ?, 0, 0, ?, ?, NULL, 'running')",
            (project_id, branch, cycles_requested, subject, started_at),
        )
        conn.commit()
        new_id = cur.lastrowid
    finally:
        conn.close()
    return get_run(db_path, new_id)


def finalize_run(
    db_path: str,
    run_id: int,
    cycles_succeeded: int,
    cycles_abandoned: int,
    pr_url: str | None = None,
    status: str = "complete",
) -> dict:
    """Update ended_at, counts, pr_url, status. Returns the updated row."""
    ended_at = _now()
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE runs SET ended_at = ?, cycles_succeeded = ?, "
            "cycles_abandoned = ?, pr_url = ?, status = ? WHERE id = ?",
            (ended_at, cycles_succeeded, cycles_abandoned, pr_url, status, run_id),
        )
        conn.commit()
    finally:
        conn.close()
    return get_run(db_path, run_id)


def get_run(db_path: str, run_id: int) -> dict | None:
    conn = get_connection(db_path)
    try:
        cur = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
        row = cur.fetchone()
        if row is None:
            return None
        cols = [c[0] for c in cur.description]
        return _row_to_dict(row, cols)
    finally:
        conn.close()


def list_runs(db_path: str, project_id: int | None = None) -> list[dict]:
    conn = get_connection(db_path)
    try:
        if project_id is None:
            cur = conn.execute("SELECT * FROM runs ORDER BY id")
        else:
            cur = conn.execute(
                "SELECT * FROM runs WHERE project_id = ? ORDER BY id",
                (project_id,),
            )
        rows = cur.fetchall()
        cols = [c[0] for c in cur.description]
        return [_row_to_dict(r, cols) for r in rows]
    finally:
        conn.close()


# ── URL → owner/repo parsing ────────────────────────────────────────────────

_URL_PATTERNS = (
    # https://github.com/owner/repo(.git)?
    re.compile(r"^https?://[^/]+/([^/]+)/([^/]+?)(?:\.git)?/?$"),
    # git@github.com:owner/repo(.git)?
    re.compile(r"^[^@]+@[^:]+:([^/]+)/([^/]+?)(?:\.git)?/?$"),
)


def parse_owner_repo(git_url: str) -> tuple[str, str]:
    """Parse owner + repo name from a git URL (https or ssh form)."""
    for pat in _URL_PATTERNS:
        m = pat.match(git_url.strip())
        if m:
            return m.group(1), m.group(2)
    raise ValueError(f"Could not parse owner/repo from git URL: {git_url!r}")


def experiment_dir_for(kaizen_root: Path, git_url: str) -> Path:
    owner, repo = parse_owner_repo(git_url)
    return kaizen_root / "experiment" / f"{owner}-{repo}"


def kaizen_root() -> Path:
    """Return the kaizen repo root (parent of scripts/)."""
    return Path(__file__).resolve().parent.parent


# ── Cleanup hook (deferred until after PR open) ────────────────────────────


def cleanup_after_pr(experiment_dir: Path) -> None:
    """Delete the experiment clone. Called by Wave 5 after PR opens."""
    from scripts.clone import cleanup_experiment

    cleanup_experiment(experiment_dir)


# ── Orchestrator ───────────────────────────────────────────────────────────


def orchestrate_run(
    db_path: str,
    git_url: str,
    cycles_requested: int,
    subject: str | None = None,
    cycle_executor=None,
) -> dict:
    """Full multi-cycle orchestration. See module docstring for the flow.

    `cycle_executor(clone_dir, project, run_row, cycle_n) -> dict` is the
    per-cycle callback. When None, `scripts.cycle.execute_cycle` is used
    (which is a Wave 4 stub — tests must inject).

    Returns a dict with the state Wave 5 needs to render and open the PR:
      run_id, project_id, branch, clone_dir, experiment_dir,
      cycles_succeeded, cycles_abandoned, cycles (list of rows),
      abandonments (list of rows), status.
    """
    # Local imports keep cycle.py / clone.py / etc. optional at import time.
    from scripts.abandonment import process_abandonment
    from scripts.clone import cleanup_experiment, clone_repo
    from scripts.cycle import (
        execute_cycle as default_executor,
    )
    from scripts.cycle import (
        list_cycles,
        record_cycle_abandoned,
        record_cycle_success,
    )
    from scripts.cycle_git import create_branch, push_branch
    from scripts.project import get_project_by_url
    from scripts.seed_atelier_in_clone import seed_all

    if cycle_executor is None:
        cycle_executor = default_executor

    # 1. Resolve project
    project = get_project_by_url(db_path, git_url)
    if project is None:
        raise RuntimeError(
            f"No project registered for {git_url!r}. Register it first:\n"
            f"  python3 scripts/project.py register {git_url}"
        )

    # 2. Clone target
    experiment_dir = experiment_dir_for(kaizen_root(), git_url)
    # H2: drop stale clone from a prior crashed run before re-cloning.
    cleanup_experiment(experiment_dir)
    clone_repo(git_url, experiment_dir, project["base_branch"])

    # 3. Seed atelier
    # M1: tear down half-initialized clone before re-raising.
    try:
        seed_all(experiment_dir)
    except Exception:
        cleanup_experiment(experiment_dir)
        raise

    # 4. Branch
    branch = create_branch(experiment_dir, subject)

    # 5. Run row
    run_row = create_run(
        db_path=db_path,
        project_id=project["id"],
        branch=branch,
        cycles_requested=cycles_requested,
        subject=subject,
    )

    # 6. Cycle loop — skip-and-continue on abandonment
    cycles_succeeded = 0
    cycles_abandoned = 0
    abandonment_rows: list[dict] = []

    # H3: finalize the run as failed so the row never sticks at status='running'.
    try:
        for cycle_n in range(1, cycles_requested + 1):
            cycle_started = _now()
            outcome = cycle_executor(experiment_dir, project, run_row, cycle_n)

            if outcome.get("status") == "success":
                record_cycle_success(
                    db_path=db_path,
                    run_id=run_row["id"],
                    cycle_n=cycle_n,
                    subject=outcome.get("subject", subject),
                    commit_sha=outcome["commit_sha"],
                    minutes_memex_slug=outcome.get("minutes_memex_slug"),
                    started_at=cycle_started,
                )
                cycles_succeeded += 1
            elif outcome.get("status") == "abandoned":
                cycle_row = record_cycle_abandoned(
                    db_path=db_path,
                    run_id=run_row["id"],
                    cycle_n=cycle_n,
                    subject=outcome.get("subject", subject),
                    started_at=cycle_started,
                )
                # _ab_markdown: caller is responsible for capturing to Memex via memex:run; not used at this layer yet.
                ab_row, _ab_markdown = process_abandonment(
                    db_path=db_path,
                    project=project,
                    run_id=run_row["id"],
                    cycle_id=cycle_row["id"],
                    cycle_n=cycle_n,
                    subject=outcome.get("subject", subject),
                    participants=outcome.get("participants", []),
                    phase_reached=outcome.get("phase_reached", "unknown"),
                    reason=outcome.get("reason", "other"),
                    detail=outcome.get("detail", ""),
                    artifacts=outcome.get("artifacts", []),
                )
                abandonment_rows.append(ab_row)
                cycles_abandoned += 1
            else:
                raise RuntimeError(
                    f"cycle_executor for cycle {cycle_n} returned unrecognised status: {outcome!r}"
                )
    except Exception:
        finalize_run(
            db_path=db_path,
            run_id=run_row["id"],
            cycles_succeeded=cycles_succeeded,
            cycles_abandoned=cycles_abandoned,
            pr_url=None,
            status="failed",
        )
        raise

    # 7. Push the branch. If push fails, leave clone in place.
    try:
        push_branch(experiment_dir, branch)
    except Exception as push_exc:
        finalized = finalize_run(
            db_path=db_path,
            run_id=run_row["id"],
            cycles_succeeded=cycles_succeeded,
            cycles_abandoned=cycles_abandoned,
            pr_url=None,
            status="failed",
        )
        return {
            "run_id": run_row["id"],
            "project_id": project["id"],
            "branch": branch,
            "clone_dir": experiment_dir,
            "experiment_dir": experiment_dir,
            "cycles_succeeded": cycles_succeeded,
            "cycles_abandoned": cycles_abandoned,
            "cycles": list_cycles(db_path, run_row["id"]),
            "abandonments": abandonment_rows,
            "status": "failed",
            "error": str(push_exc),
            "run": finalized,
        }

    # 8. Finalize
    finalized = finalize_run(
        db_path=db_path,
        run_id=run_row["id"],
        cycles_succeeded=cycles_succeeded,
        cycles_abandoned=cycles_abandoned,
        pr_url=None,
        status="complete",
    )

    # 9. Cleanup is deferred — caller invokes cleanup_after_pr() once
    # Wave 5's PR-open step succeeds.

    return {
        "run_id": run_row["id"],
        "project_id": project["id"],
        "branch": branch,
        "clone_dir": experiment_dir,
        "experiment_dir": experiment_dir,
        "cycles_succeeded": cycles_succeeded,
        "cycles_abandoned": cycles_abandoned,
        "cycles": list_cycles(db_path, run_row["id"]),
        "abandonments": abandonment_rows,
        "status": "complete",
        "run": finalized,
    }


# ── CLI (dev-test only; real entry is `kaizen:improve` in Wave 7) ──────────


def main(argv: list[str]) -> int:
    if not argv or argv[0] != "orchestrate":
        print('Usage: run.py orchestrate <git-url> [--cycles N] [--subject "..."]', file=sys.stderr)
        return 1

    rest = argv[1:]
    if not rest:
        print("Missing <git-url>", file=sys.stderr)
        return 1

    git_url = rest[0]
    cycles = 1
    subject = None
    i = 1
    while i < len(rest):
        if rest[i] == "--cycles" and i + 1 < len(rest):
            cycles = int(rest[i + 1])
            i += 2
        elif rest[i] == "--subject" and i + 1 < len(rest):
            subject = rest[i + 1]
            i += 2
        else:
            print(f"Unknown arg: {rest[i]!r}", file=sys.stderr)
            return 1

    db_path = ".ai/memex.db"
    import json

    result = orchestrate_run(
        db_path=db_path,
        git_url=git_url,
        cycles_requested=cycles,
        subject=subject,
    )
    # Path objects aren't JSON serialisable
    result = {k: (str(v) if isinstance(v, Path) else v) for k, v in result.items()}
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
