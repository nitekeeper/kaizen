"""Automated three-layer cleanup of orphan Claude Code teammates.

Promotes the manual recipe from
`docs/runbooks/orphan-teammate-cleanup.md` (Steps 1-3) into a callable
helper. Reuses `scripts.sweep_leaked_teams.find_orphan_team_ids` for
Layer 3 (config) discovery -- this module is purely the cleanup driver;
all bridge-DB knowledge stays in `sweep_leaked_teams`.

Layered scope (a spawned teammate is THREE resources):

  - Layer 1 (process): `pgrep -af '--agent-id <team_id_pattern>'` to
    enumerate live `claude` teammate processes. Optional `--apply`
    sends SIGTERM to each PID.
  - Layer 2 (pane): `tmux list-panes -a` filtered so `pane_pid`
    matches a PID from Layer 1. Optional `--apply` calls
    `tmux kill-pane` per matching pane_id.
  - Layer 3 (config): delegates to
    `sweep_leaked_teams.find_orphan_team_ids()`, then `rm -rf`s each
    `~/.claude/teams/<team_id>/` directory whose team_id matches the
    supplied pattern.

CRITICAL safety gate:

  If `dry_run=False` AND `team_id_pattern is None`, the function
  raises `ValueError` BEFORE running any subprocess. Apply mode with
  no scope would broadcast `pkill`/`tmux kill-pane`/`rm -rf` across
  every Claude teammate on the machine — including teammates owned by
  unrelated live sessions. The gate fires first to make the failure
  loud and contained.

CLI: `python3 -m scripts.cleanup_orphans [--apply] [--pattern PATTERN]
[--verbose]`. Default is dry-run; `--apply` flips it to destructive
mode. `--pattern` is the substring matched against `--agent-id` arg
values (Layer 1) and team_id directory names (Layer 3); it is
mandatory whenever `--apply` is set.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from scripts.sweep_leaked_teams import find_orphan_team_ids

# Default config-layer root. Kept module-level so tests can monkeypatch
# it without touching the user's real `~/.claude/teams/`.
_DEFAULT_TEAMS_DIR = Path.home() / ".claude" / "teams"

# Default bridge DB location — same default as bridge_db / sweep
# scripts. Module-level so tests can override.
_DEFAULT_BRIDGE_DB = ".ai/bridge.db"


def _pgrep_agent_processes(pattern: str) -> list[tuple[int, str]]:
    """Layer 1 — return list of (pid, full_command) for matching procs.

    Matches `claude` processes whose argv contains `--agent-id` AND
    `<pattern>`. Empty list on no matches. `pgrep -af` returns the
    PID + full command line on each line.
    """
    # Use a regex that requires `--agent-id` AND the pattern to appear
    # in the argv. `pgrep -af` returns 1 on no-match — treat that as
    # empty rather than an error.
    expr = rf"--agent-id\s+\S*{pattern}"
    proc = subprocess.run(
        ["pgrep", "-af", expr],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"pgrep failed (exit {proc.returncode}): {proc.stderr.strip()}")
    out: list[tuple[int, str]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_str, _, cmd = line.partition(" ")
        try:
            out.append((int(pid_str), cmd))
        except ValueError:
            continue
    return out


def _kill_pids(pids: list[int]) -> dict[int, str]:
    """SIGTERM each pid; return {pid: 'killed'|'error: <msg>'}."""
    results: dict[int, str] = {}
    for pid in pids:
        try:
            os.kill(pid, 15)  # SIGTERM
            results[pid] = "killed"
        except ProcessLookupError:
            results[pid] = "error: no such process"
        except PermissionError as e:
            results[pid] = f"error: permission denied ({e})"
    return results


def _tmux_panes_for_pids(pids: set[int]) -> list[tuple[str, int]]:
    """Layer 2 — return list of (pane_id, pane_pid) for panes whose
    pane_pid is in the supplied set. Empty list on no matches OR if
    tmux is not installed / no server running."""
    if not pids:
        return []
    proc = subprocess.run(
        ["tmux", "list-panes", "-a", "-F", "#{pane_id} #{pane_pid}"],
        capture_output=True,
        text=True,
        check=False,
    )
    # tmux exits non-zero when no server is running — treat as "no
    # panes" rather than crashing the cleanup.
    if proc.returncode != 0:
        return []
    out: list[tuple[str, int]] = []
    for line in proc.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) != 2:
            continue
        try:
            pane_pid = int(parts[1])
        except ValueError:
            continue
        if pane_pid in pids:
            out.append((parts[0], pane_pid))
    return out


def _kill_panes(pane_ids: list[str]) -> dict[str, str]:
    """tmux kill-pane each pane_id; return {pane_id: 'killed'|'error: …'}."""
    results: dict[str, str] = {}
    for pane in pane_ids:
        proc = subprocess.run(
            ["tmux", "kill-pane", "-t", pane],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            results[pane] = "killed"
        else:
            results[pane] = f"error: {proc.stderr.strip() or 'tmux non-zero exit'}"
    return results


def _find_config_orphans(
    bridge_db_path: str | Path,
    team_id_pattern: str | None,
) -> list[tuple[int, str]]:
    """Layer 3 discovery — delegate to sweep_leaked_teams, optionally
    filter by pattern substring against team_id."""
    orphans = find_orphan_team_ids(bridge_db_path)
    if team_id_pattern is None:
        return orphans
    return [(run, tid) for run, tid in orphans if team_id_pattern in tid]


def _rm_config_dirs(
    team_ids: list[str],
    teams_dir: Path,
) -> dict[str, str]:
    """rm -rf each team_id directory under teams_dir."""
    results: dict[str, str] = {}
    for tid in team_ids:
        target = teams_dir / tid
        if not target.exists():
            results[tid] = "skipped: not present"
            continue
        try:
            shutil.rmtree(target)
            results[tid] = "removed"
        except OSError as e:
            results[tid] = f"error: {e}"
    return results


def cleanup_orphans(
    team_id_pattern: str | None = None,
    dry_run: bool = True,
    *,
    bridge_db_path: str | Path = _DEFAULT_BRIDGE_DB,
    teams_dir: Path | None = None,
    verbose: bool = False,
) -> dict:
    """Three-layer orphan teammate cleanup.

    Args:
        team_id_pattern: substring matched against the `--agent-id`
            argv (Layer 1) and team_id directory names (Layer 3).
            MUST be set whenever `dry_run=False`. If `None` AND
            `dry_run=True`, the function reports across all
            teammate-shaped processes / panes / orphan configs.
        dry_run: if True (default), only enumerates and returns a
            plan dict — NO `pgrep`, `tmux`, `kill`, or `rm` is
            invoked. The function does NOT spawn any subprocess in
            dry-run mode.
        bridge_db_path: bridge DB path passed through to
            `find_orphan_team_ids`.
        teams_dir: override for `~/.claude/teams/` (testing).
        verbose: include per-PID / per-pane / per-team status in the
            return dict.

    Returns:
        dict with keys:
          - mode: "dry-run" | "applied"
          - pattern: the supplied pattern (or None)
          - layer1: {pids: [...], (apply-only) kill_results: {...}}
          - layer2: {panes: [...], (apply-only) kill_results: {...}}
          - layer3: {team_ids: [...], (apply-only) rm_results: {...}}

    Raises:
        ValueError: if `dry_run=False` AND `team_id_pattern is None`.
            Raised BEFORE any subprocess is invoked.
    """
    # ---- CRITICAL SAFETY GATE ----------------------------------------
    # Must run BEFORE any pgrep/tmux/kill/rm call. An unscoped apply
    # would broadcast destructive actions across every Claude teammate
    # on the host, including teammates owned by unrelated live
    # sessions.
    if not dry_run and team_id_pattern is None:
        raise ValueError(
            "refusing to apply cleanup with no pattern — pass team_id_pattern explicitly"
        )
    # -------------------------------------------------------------------

    teams_dir = teams_dir or _DEFAULT_TEAMS_DIR
    mode = "dry-run" if dry_run else "applied"
    report: dict = {"mode": mode, "pattern": team_id_pattern}

    if dry_run:
        # DRY-RUN PATH: NO subprocess calls. The runbook is explicit
        # that even reading processes/panes should not happen in plan
        # mode — the user gets the *intent* from the bridge DB only.
        plan_team_ids = [tid for _, tid in _find_config_orphans(bridge_db_path, team_id_pattern)]
        report["layer1"] = {
            "pids": [],
            "note": "dry-run: pgrep not invoked; rerun with --apply --pattern to enumerate",
        }
        report["layer2"] = {
            "panes": [],
            "note": "dry-run: tmux not invoked; rerun with --apply --pattern to enumerate",
        }
        report["layer3"] = {
            "team_ids": plan_team_ids,
            "note": "dry-run: rm not invoked",
        }
        if verbose:
            report["verbose"] = True
        return report

    # APPLY PATH: pattern is guaranteed non-None by the safety gate.
    assert team_id_pattern is not None  # for type-checkers

    # Layer 1.
    procs = _pgrep_agent_processes(team_id_pattern)
    pids = [pid for pid, _ in procs]
    kill_results = _kill_pids(pids)
    layer1: dict = {"pids": pids}
    if verbose:
        layer1["commands"] = dict(procs)
    layer1["kill_results"] = kill_results
    report["layer1"] = layer1

    # Layer 2 — derived from Layer 1 PIDs so we never blast unrelated
    # panes. Use the set of PIDs we observed (not the set we
    # successfully killed) — pane closure may still be needed for
    # already-exited processes whose shell stayed open.
    panes = _tmux_panes_for_pids(set(pids))
    pane_ids = [pane_id for pane_id, _ in panes]
    pane_kill = _kill_panes(pane_ids)
    layer2: dict = {"panes": pane_ids}
    if verbose:
        layer2["pane_pids"] = dict(panes)
    layer2["kill_results"] = pane_kill
    report["layer2"] = layer2

    # Layer 3 — delegate discovery to sweep_leaked_teams, then rm -rf.
    orphan_team_ids = [tid for _, tid in _find_config_orphans(bridge_db_path, team_id_pattern)]
    rm_results = _rm_config_dirs(orphan_team_ids, teams_dir)
    report["layer3"] = {
        "team_ids": orphan_team_ids,
        "rm_results": rm_results,
    }

    return report


def _format_report(report: dict) -> str:
    """Render a plan/apply report as plain text for the CLI."""
    lines = [
        f"cleanup_orphans: mode={report['mode']} pattern={report.get('pattern')!r}",
        "  Layer 1 (processes):",
    ]
    layer1 = report.get("layer1", {})
    lines.append(f"    pids: {layer1.get('pids', [])}")
    if "kill_results" in layer1:
        for pid, status in layer1["kill_results"].items():
            lines.append(f"      {pid}: {status}")
    if "note" in layer1:
        lines.append(f"    note: {layer1['note']}")

    lines.append("  Layer 2 (panes):")
    layer2 = report.get("layer2", {})
    lines.append(f"    panes: {layer2.get('panes', [])}")
    if "kill_results" in layer2:
        for pane, status in layer2["kill_results"].items():
            lines.append(f"      {pane}: {status}")
    if "note" in layer2:
        lines.append(f"    note: {layer2['note']}")

    lines.append("  Layer 3 (configs):")
    layer3 = report.get("layer3", {})
    lines.append(f"    team_ids: {layer3.get('team_ids', [])}")
    if "rm_results" in layer3:
        for tid, status in layer3["rm_results"].items():
            lines.append(f"      {tid}: {status}")
    if "note" in layer3:
        lines.append(f"    note: {layer3['note']}")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="cleanup_orphans",
        description=(
            "Three-layer orphan-teammate cleanup driver. "
            "Promotes the manual recipe in "
            "docs/runbooks/orphan-teammate-cleanup.md into a callable helper. "
            "Default is dry-run; pass --apply to actually kill/rm. "
            "--pattern is mandatory whenever --apply is set."
        ),
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Destructive mode — invoke kill/tmux kill-pane/rm -rf. Requires --pattern.",
    )
    ap.add_argument(
        "--pattern",
        default=None,
        help=(
            "Substring matched against --agent-id argv (Layer 1) and team_id directory names "
            "(Layer 3). Required when --apply is set."
        ),
    )
    ap.add_argument(
        "--bridge-db",
        default=_DEFAULT_BRIDGE_DB,
        dest="bridge_db",
        help="Bridge DB path (default .ai/bridge.db).",
    )
    ap.add_argument(
        "--verbose",
        action="store_true",
        help="Include per-PID / per-pane / per-team detail in the report.",
    )
    args = ap.parse_args(argv)

    try:
        report = cleanup_orphans(
            team_id_pattern=args.pattern,
            dry_run=not args.apply,
            bridge_db_path=args.bridge_db,
            verbose=args.verbose,
        )
    except ValueError as e:
        print(f"cleanup_orphans: {e}", file=sys.stderr)
        return 2

    print(_format_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
