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

import argparse
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


def update_run_branch(
    db_path: str,
    run_id: int,
    branch: str | None,
) -> None:
    """Update `runs.branch` for `run_id`.

    Used by the team-mode bridge entry path:

      * After `create_branch(...)` succeeds in `orchestrate_run`, this
        is called with the real branch name to transition from the
        `'<pending>'` placeholder seeded by `create-run-only`.
      * In the run-loop's outer `except Exception` block, this is
        called with `branch=None`. The schema column is `TEXT NOT NULL`
        (migration 001 line 18), so passing `None` cannot land as SQL
        NULL — instead the sentinel string `'<failed>'` is persisted
        (MAJOR-NEW-BRANCH-NOT-NULL). `scripts.pr.render_pr_body`
        refuses to render against either placeholder.

    Raises ValueError if `run_id` does not exist.
    """
    # The schema declares `branch TEXT NOT NULL` (migrations/001 line 18),
    # which is exactly the constraint the `'<failed>'` sentinel
    # accommodates. Do NOT modify the migration — the sentinel IS the
    # fix.
    persisted = "<failed>" if branch is None else branch
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "UPDATE runs SET branch = ? WHERE id = ?",
            (persisted, run_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"run_id={run_id} not found")
        conn.commit()
    finally:
        conn.close()


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


# B-INJ-1 (review round 2): shell-metacharacter denylist for git URLs.
# Defence in depth — the SKILL prose now single-quotes every <placeholder>,
# but if a user ever pastes a malicious URL into a context that strips
# the quotes (e.g. an integration that builds the command via `subprocess`
# with `shell=True`), `_cmd_create_run_only` will reject it here BEFORE
# touching the projects table.
#
# Characters explicitly forbidden in git URLs we accept:
#   ; | & $ ` ( ) < > newline carriage-return tab " ' \ space
# Plus any control character (< 0x20) and any backslash-escaped form.
# This is intentionally restrictive — legitimate https/ssh URLs to
# GitHub/GitLab/Bitbucket cannot contain any of these.
_URL_SHELL_METACHARS = frozenset(";|&$`()<>\n\r\t \"'\\")


def validate_git_url(git_url: str) -> None:
    """Reject git URLs that contain shell metacharacters or control chars.

    Belt-and-braces against shell injection in the SKILL-prose launch
    sequence (B-INJ-1). Single-quoted shell substitution already
    neutralises every metacharacter EXCEPT a literal single quote (and
    SQL/SKILL prose accepting agent-authored URLs is harder to audit),
    so this gate refuses ALL shell metacharacters up front.

    Raises ValueError. Caller is responsible for converting to whatever
    exit/error contract their entry point uses.
    """
    if not isinstance(git_url, str) or not git_url:
        raise ValueError(f"git_url must be a non-empty string; got {git_url!r}")
    # Reject any control character (\x00-\x1F) and DEL.
    for ch in git_url:
        if ord(ch) < 0x20 or ch == "\x7f":
            raise ValueError(f"git_url contains control character {ch!r}; rejected")
        if ch in _URL_SHELL_METACHARS:
            raise ValueError(
                f"git_url contains shell metacharacter {ch!r}; rejected "
                "(allowed forms: https://host/owner/repo[.git] or "
                "git@host:owner/repo[.git])"
            )
    # Final sanity: must parse into owner/repo via the same patterns the
    # rest of the codebase uses. This catches well-formed-but-bogus URLs.
    parse_owner_repo(git_url)


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


def _bridge_timeout_to_abandoned_outcome(exc, *, subject, branch) -> dict:
    """kaizen#91 — convert a bridge timeout/stall into a cycle-abandoned outcome.

    Reads the read-first snapshot attached to the exception (``exc.snapshot`` —
    ``None`` for the single-row ``_request`` path) and builds the structured
    ``status='abandoned'`` dict the cycle loop already records + skip-and-
    continues on. Also emits a structured stderr line so the capture reaches a
    human via the run_bridged log even if the later DB write fails.

    Capture-only: it never resumes, commits, or pushes the survived work — the
    branch name is a manual-recovery POINTER. ``reason='other'`` + a greppable
    detail prefix avoids a schema migration for a dedicated reason; the phase
    is a best-effort ``'implementation'`` default (the bridge layer does not
    carry the cycle phase — a future refinement could thread the real phase).
    """
    snapshot = getattr(exc, "snapshot", None)
    exc_name = type(exc).__name__
    if snapshot is not None:
        classification = snapshot.classification
        recipients = (
            ", ".join(snapshot.pending_recipients) if snapshot.pending_recipients else "(none)"
        )
        surviving_summary = (
            f"{snapshot.completed_count} of {snapshot.total} bridge rows completed "
            f"before the {exc_name}; {snapshot.pending_count} pending "
            f"(recipients: {recipients})"
            + (
                f"; {snapshot.soft_dropped_count} soft-dropped"
                if snapshot.soft_dropped_count
                else ""
            )
        )
        participants = list(snapshot.pending_recipients)
    else:
        classification = "true_stall"
        surviving_summary = f"single-row bridge call raised {exc_name}; no batch progress snapshot"
        participants = []

    detail = f"bridge {exc_name} ({classification}): {exc}. {surviving_summary}"

    # Always-available capture surface: the run_bridged subprocess log that the
    # orchestrator session tails. Fires even if the DB write below fails.
    print(
        f"[kaizen#91] bridge abandonment captured — {classification}; "
        f"recoverable branch: {branch}; {surviving_summary}",
        file=sys.stderr,
    )

    return {
        "status": "abandoned",
        "phase_reached": "implementation",
        "reason": "other",
        "detail": detail,
        "participants": participants,
        "artifacts": [branch] if branch else [],
        "recoverable_artifact": branch,
        "progress_classification": classification,
        "surviving_summary": surviving_summary,
    }


def orchestrate_run(
    db_path: str,
    git_url: str,
    cycles_requested: int,
    subject: str | None = None,
    cycle_executor=None,
    mode: str = "subagent",
    *,
    tools_provider=None,
    run_id: int | None = None,
) -> dict:
    """Full multi-cycle orchestration. See module docstring for the flow.

    `cycle_executor(clone_dir, project, run_row, cycle_n) -> dict` is the
    per-cycle callback. When None, the executor is selected based on `mode`:
      - mode='subagent' (default): uses `scripts.cycle.execute_cycle` (Wave 4
        stub — tests must inject a real executor).
      - mode='team': uses `scripts.team_executor.team_cycle_executor`, which
        requires CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 in the environment.

    Passing an explicit `cycle_executor` overrides `mode` selection — useful
    for testing.

    `tools_provider` is a `Callable[[Path, dict, dict, int], TeamTools] | None`
    invoked once per cycle to build the `TeamTools` wrapper passed as the
    `tools=` keyword arg into `team_cycle_executor`. It is REQUIRED when
    `mode='team'` (the team executor cannot construct CC session-tool
    wrappers itself — Python cannot directly call Claude Code session
    tools). When `mode='subagent'` `tools_provider` is silently IGNORED for
    minimum surprise. When `mode='team'` and `tools_provider` is None, the
    orchestrator raises `ValueError` BEFORE any clone / seed / branch / run
    row side effect so the failure leaves no garbage on disk or in the DB.

    Returns a dict with the state Wave 5 needs to render and open the PR:
      run_id, project_id, branch, clone_dir, experiment_dir,
      cycles_succeeded, cycles_abandoned, cycles (list of rows),
      abandonments (list of rows), status, mode.
    """
    # Local imports keep cycle.py / clone.py / etc. optional at import time.
    from scripts.abandonment import VALID_PHASES, VALID_REASONS, process_abandonment
    from scripts.cc_tool_bridge import BridgeStallError, BridgeTimeoutError
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

    # Did the caller supply their own executor? Used by the bridge guard
    # below — explicit-executor callers (tests injecting stubs) bypass the
    # tools_provider requirement because they are wiring the cycle work
    # themselves and may not need the real team_cycle_executor at all.
    executor_was_injected = cycle_executor is not None
    if cycle_executor is None:
        if mode == "team":
            from scripts.team_executor import team_cycle_executor

            cycle_executor = team_cycle_executor
        else:
            cycle_executor = default_executor

    # 1. Resolve project
    project = get_project_by_url(db_path, git_url)
    if project is None:
        raise RuntimeError(
            f"No project registered for {git_url!r}. Register it first:\n"
            f"  python3 scripts/project.py register {git_url}"
        )

    # 1b. Bridge guard — team mode without a tools_provider would crash deep
    # inside `team_cycle_executor`'s preflight (tools is None →
    # TeamToolsUnavailableError) AFTER cloning, seeding atelier, branching,
    # and creating a run row. Fire here so the failure leaves NO side
    # effects on disk or in the DB.
    #
    # The guard ONLY fires when the caller did NOT inject their own
    # `cycle_executor`. Explicit-executor callers (tests, custom drivers)
    # are wiring the cycle themselves; for them the tools_provider is
    # optional and the existing 4-positional-arg signature still works.
    # This preserves backward compatibility with every existing call site
    # that injects a stub executor for `mode='team'` to test mode plumbing.
    if mode == "team" and tools_provider is None and not executor_was_injected:
        raise ValueError(
            "mode='team' requires a tools_provider callable to construct the "
            "TeamTools wrapper for each cycle. Without it, team_cycle_executor "
            "crashes deep inside the cycle. Pass "
            "tools_provider=lambda clone_dir, project, run_row, cycle_n: ... "
            "or use mode='subagent' instead."
        )

    # M1 (review round 1): Bridge entry path's `run_id` existence guard
    # MUST fire BEFORE any clone/seed/branch side effect. A bad run_id
    # used to escape past clone_repo + seed_all + create_branch, leaving
    # a populated `experiment/` directory on disk.
    #
    # m-DEAD (review round 2): we deliberately discard the fetched row
    # here. The row is re-fetched inside the outer-try block AFTER
    # `update_run_branch(...)` has transitioned `branch` from
    # `'<pending>'` to the real name — at THAT point downstream code
    # needs the updated row, not the stale pre-update snapshot. Holding
    # the prefetched row and using it later would let agents observe
    # the stale `branch='<pending>'` value. Existence-check only.
    if run_id is not None and get_run(db_path, run_id) is None:
        raise ValueError(f"run_id={run_id} not found in {db_path}")

    # 2. Clone target
    experiment_dir = experiment_dir_for(kaizen_root(), git_url)
    # H2: drop stale clone from a prior crashed run before re-cloning.
    cleanup_experiment(experiment_dir)

    # 6. Cycle loop — skip-and-continue on abandonment
    cycles_succeeded = 0
    cycles_abandoned = 0
    abandonment_rows: list[dict] = []
    run_row: dict | None = None
    branch: str | None = None

    # M2 (review round 1): the outer try MUST cover clone/seed/branch,
    # not just the cycle loop. Otherwise a failure in any of those
    # leaves `runs.branch='<pending>'` and `runs.status='running'`
    # permanently — the exact hazard the `<failed>` sentinel was meant
    # to prevent.
    try:
        clone_repo(git_url, experiment_dir, project["base_branch"])

        # 3. Seed atelier
        # M1 (original): tear down half-initialized clone before re-raising.
        try:
            seed_all(experiment_dir)
        except Exception:
            cleanup_experiment(experiment_dir)
            raise

        # 4. Branch
        branch = create_branch(experiment_dir, subject)

        # 5. Run row.
        #
        # Bridge entry path: when `run_id` is supplied, the row was already
        # created by `scripts/run.py create-run-only` with the placeholder
        # `branch='<pending>'`. Transition the branch column to the real
        # name we just created. The placeholder is observable for at most
        # the time between `create-run-only` returning and this line;
        # `scripts/pr.py::render_pr_body` refuses to render against
        # `'<pending>'`, `'<failed>'`, `None`, or `''`.
        if run_id is None:
            run_row = create_run(
                db_path=db_path,
                project_id=project["id"],
                branch=branch,
                cycles_requested=cycles_requested,
                subject=subject,
            )
        else:
            # Persist the real branch immediately; refresh the local row
            # so downstream code sees the updated value.
            update_run_branch(db_path, run_id, branch)
            run_row = get_run(db_path, run_id)

        # H3: finalize the run as failed so the row never sticks at status='running'.
        for cycle_n in range(1, cycles_requested + 1):
            cycle_started = _now()
            # Team mode threads a per-cycle TeamTools wrapper into the
            # executor via the `tools=` kwarg. Subagent mode preserves the
            # 4-positional-arg call signature for backward compatibility
            # with any existing executor callable (including the default
            # `scripts.cycle.execute_cycle`).
            #
            # kaizen#91 — read-first capture. A bridge per-call timeout /
            # cycle wall-clock / heartbeat stall raises BridgeTimeoutError or
            # BridgeStallError from deep inside the executor's bridge dispatch.
            # Without this guard it propagates to the blanket `except Exception`
            # below, which blanks the branch to '<failed>', finalizes
            # status='failed', and re-raises — crashing the whole run and
            # discarding the teammate work that DID complete before the trip
            # (the run-53 failure mode). Convert it into a proper cycle
            # abandonment instead: the in-flight work is captured into the
            # report (e.snapshot), the next cycle still runs (working-rule 3),
            # and the run finalizes with a PR referencing the report rather
            # than a bare crash. team_cycle_executor's `finally` has already
            # torn down the team before the exception reached us, so no
            # teammate leaks across into the next cycle.
            try:
                if mode == "team" and tools_provider is not None:
                    tools = tools_provider(experiment_dir, project, run_row, cycle_n)
                    outcome = cycle_executor(experiment_dir, project, run_row, cycle_n, tools=tools)
                else:
                    outcome = cycle_executor(experiment_dir, project, run_row, cycle_n)
            except (BridgeTimeoutError, BridgeStallError) as bridge_exc:
                outcome = _bridge_timeout_to_abandoned_outcome(
                    bridge_exc, subject=subject, branch=branch
                )

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
                # Fail-loud allowlist guards: the schema CHECK constraints
                # (migration 004) only permit specific values for phase_reached
                # and reason. ANY out-of-set value (None, "unknown", "bogus",
                # typos) would crash later with sqlite3.IntegrityError at
                # INSERT INTO abandonments time, *after* the cycle's work was
                # already done. Validating against the canonical frozensets
                # imported from scripts.abandonment guarantees we fail before
                # any DB write and that the error message names both the
                # offending cycle and the full set of legal values.
                phase_reached = outcome.get("phase_reached")
                reason = outcome.get("reason")
                if phase_reached not in VALID_PHASES:
                    raise ValueError(
                        f"cycle {cycle_n} outcome has invalid 'phase_reached'={phase_reached!r}; "
                        f"valid values per migration 004: {sorted(VALID_PHASES)}"
                    )
                if reason not in VALID_REASONS:
                    raise ValueError(
                        f"cycle {cycle_n} outcome has invalid 'reason'={reason!r}; "
                        f"valid values per migration 004: {sorted(VALID_REASONS)}"
                    )
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
                    phase_reached=phase_reached,
                    reason=reason,
                    detail=outcome.get("detail", ""),
                    artifacts=outcome.get("artifacts", []),
                    # Phase 5b' review-loop fields — only populated by the
                    # cycle executor when reason='review_unrecoverable'.
                    # Defaulting to None preserves legacy abandonment shape
                    # for all other reasons.
                    review_iteration_count=outcome.get("review_iteration_count"),
                    unresolved_findings=outcome.get("unresolved_findings"),
                    convergence_summary=outcome.get("convergence_summary"),
                    reviewer_attribution=outcome.get("reviewer_attribution"),
                    # kaizen#91 — recoverable-artifact pointer for bridge-timeout
                    # abandonments. None for every other abandonment reason, so
                    # the report shape is unchanged for legacy cases.
                    recoverable_artifact=outcome.get("recoverable_artifact"),
                    progress_classification=outcome.get("progress_classification"),
                    surviving_summary=outcome.get("surviving_summary"),
                )
                abandonment_rows.append(ab_row)
                cycles_abandoned += 1
            else:
                raise RuntimeError(
                    f"cycle_executor for cycle {cycle_n} returned unrecognised status: {outcome!r}"
                )
    except Exception:
        # Bridge entry path (M2/M3 fix): blank out the branch column so
        # a later manual `pr.py` invocation cannot accidentally try to
        # render a PR against this aborted run.
        # `update_run_branch(branch=None)` writes the `'<failed>'`
        # sentinel (the schema NOT NULL constraint forbids a literal
        # NULL). Best-effort — never mask the original cycle exception.
        #
        # This block now covers ALL pre-push failures (clone, seed,
        # branch, cycle loop) — M2 fix.
        import contextlib

        if run_id is not None:
            with contextlib.suppress(Exception):
                update_run_branch(db_path, run_id, None)
        # `run_row` may be None if the failure happened in clone/seed/
        # branch — those run BEFORE the run row is established. In
        # that case finalize_run has nothing to update (no row to
        # finalize for the legacy entry path; the bridge entry path
        # already has its row, which run_id covers).
        if run_row is not None:
            with contextlib.suppress(Exception):
                finalize_run(
                    db_path=db_path,
                    run_id=run_row["id"],
                    cycles_succeeded=cycles_succeeded,
                    cycles_abandoned=cycles_abandoned,
                    pr_url=None,
                    status="failed",
                )
        elif run_id is not None:
            # Bridge entry path: the run row exists (created by
            # create-run-only) even when our local run_row is None
            # because the failure happened before we re-fetched it.
            with contextlib.suppress(Exception):
                finalize_run(
                    db_path=db_path,
                    run_id=run_id,
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
        # M3 (review round 1): bridge entry path must blank the branch
        # column too — a later manual `pr.py` invocation against this
        # run would otherwise try to render against a branch name that
        # may not exist on the remote. The `<failed>` sentinel makes
        # render_pr_body refuse before it calls `gh`.
        if run_id is not None:
            import contextlib

            with contextlib.suppress(Exception):
                update_run_branch(db_path, run_id, None)
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
            "mode": mode,
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
        "mode": mode,
        "run": finalized,
    }


# ── CLI (dev-test only; real entry is `kaizen:improve` in Wave 7) ──────────


def _cmd_create_run_only(argv: list[str], db_path: str = ".ai/memex.db") -> int:
    """`create-run-only <git_url> <cycles> <subject>` — bridge entry helper.

    The orchestrating Claude session calls this BEFORE spawning the
    detached Python (`scripts/run_bridged.py`). It looks up the project
    by URL — **fails loudly** with a registration hint when no project
    matches (Decision D3: fail loudly, do NOT auto-register) — then
    creates a `runs` row with the placeholder `branch='<pending>'`.

    On success: prints ONLY the new run_id on stdout (no other text),
    exits 0. `scripts/run_bridged.py` reads this single line into a
    shell variable.
    """
    from scripts.project import get_project_by_url

    ap = argparse.ArgumentParser(prog="run.py create-run-only", add_help=False)
    ap.add_argument("git_url")
    ap.add_argument("cycles", type=int)
    ap.add_argument("subject", nargs="?", default=None)
    args = ap.parse_args(argv)
    git_url = args.git_url
    cycles = args.cycles
    subject = args.subject

    # B-INJ-1 (review round 2): refuse URLs with shell metacharacters
    # BEFORE any DB lookup. Defence in depth — the SKILL prose
    # single-quotes substitutions, but a misconfigured caller (e.g.
    # subprocess with shell=True) could still feed us a malicious URL.
    try:
        validate_git_url(git_url)
    except ValueError as e:
        print(f"create-run-only: invalid git_url: {e}", file=sys.stderr)
        return 1

    project = get_project_by_url(db_path, git_url)
    if project is None:
        # MINOR-CREATE-RUN-ONLY-AUTOREGISTER → Decision D3: fail loudly.
        # Auto-registration would mask URL typos (a misspelled URL would
        # silently create a phantom projects row, clone the wrong
        # target, and abandon-loop forever).
        print(
            f"No project registered for {git_url!r}.\n"
            f"  Register it first: python3 scripts/project.py register {git_url}",
            file=sys.stderr,
        )
        return 1

    run_row = create_run(
        db_path=db_path,
        project_id=project["id"],
        branch="<pending>",
        cycles_requested=cycles,
        subject=subject,
    )
    # stdout MUST contain only the run_id on a single line — the
    # detached spawner uses `RUN_ID=$(... create-run-only ...)`.
    print(run_row["id"])
    return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] == "create-run-only":
        return _cmd_create_run_only(argv[1:])

    if not argv or argv[0] != "orchestrate":
        print(
            "Usage:\n"
            '  run.py orchestrate <git-url> [--cycles N] [--subject "..."] [--mode subagent|team]\n'
            "  run.py create-run-only <git-url> <cycles> [<subject>]",
            file=sys.stderr,
        )
        return 1

    parser = argparse.ArgumentParser(prog="run.py orchestrate")
    parser.add_argument("git_url")
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--subject", default=None)
    parser.add_argument("--mode", choices=("subagent", "team"), default="subagent")
    ns = parser.parse_args(argv[1:])

    db_path = ".ai/memex.db"
    import json

    result = orchestrate_run(
        db_path=db_path,
        git_url=ns.git_url,
        cycles_requested=ns.cycles,
        subject=ns.subject,
        mode=ns.mode,
    )
    # Path objects aren't JSON serialisable
    result = {k: (str(v) if isinstance(v, Path) else v) for k, v in result.items()}
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
