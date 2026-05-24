"""Detached-subprocess entry point for team-mode improvement cycles.

The orchestrating Claude session (S1) spawns this script via `nohup
python3 -m scripts.run_bridged ... &` AFTER it has created the run
row via `python3 -m scripts.run create-run-only ...`. S1 then enters
its bridge-poll tool-loop while this process drives `orchestrate_run`
in the background.

Per the python-cc-tool-bridge design (Rev 4):

  1. Validate required env vars at startup; fail loudly listing missing
     vars BEFORE any clone work.
  2. Bootstrap the bridge DB (`scripts.bridge_db.bootstrap`).
  3. Call `orchestrate_run` with `run_id=<from argv>`, `mode='team'`,
     `tools_provider=queue_bridge_provider(...)`.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Iterable

from scripts.bridge_db import bootstrap

# Hard required env vars per the design's "Env inheritance contract"
# table. Each must be set AND non-empty.
_REQUIRED_ENV: tuple[str, ...] = (
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS",
    "PATH",
    "HOME",
    "PYTHONPATH",
)


def _missing_required_env() -> list[str]:
    missing: list[str] = []
    for var in _REQUIRED_ENV:
        if not os.environ.get(var):
            missing.append(var)
    return missing


def _gh_token_present() -> bool:
    """`gh pr create` needs either GH_TOKEN/GITHUB_TOKEN OR a working
    `gh auth status`. Accept either."""
    if os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"):
        return True
    gh = shutil.which("gh")
    if gh is None:
        return False
    try:
        result = subprocess.run(
            [gh, "auth", "status"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except Exception:
        # Defensive: any failure invoking gh auth status (timeout,
        # permission issue, etc.) is treated as "no auth available."
        return False
    return result.returncode == 0


def _check_path_for_tools(tools: Iterable[str]) -> list[str]:
    """Return any tools NOT discoverable on PATH."""
    return [t for t in tools if shutil.which(t) is None]


def validate_environment() -> None:
    """Validate all required env vars + PATH tools BEFORE any clone work.

    Raises SystemExit with a single-line diagnostic listing every
    missing var/tool, exit code 2.
    """
    problems: list[str] = []
    missing = _missing_required_env()
    if missing:
        problems.append("missing required env vars: " + ", ".join(missing))

    if os.environ.get("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS") not in ("1", "true", "True"):
        problems.append(
            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS must equal '1' "
            f"(got {os.environ.get('CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS')!r})"
        )

    # PATH-resident tools used by clone, CI mirror, push, PR open.
    # m5 (review round 1): include `pytest` and `ruff` per the design
    # doc's env-inheritance contract — they are required by the
    # in-clone CI mirror that the cycle implementer must pass.
    missing_tools = _check_path_for_tools(["git", "gh", "pytest", "ruff"])
    if missing_tools:
        problems.append("missing tools on PATH: " + ", ".join(missing_tools))

    if not _gh_token_present():
        problems.append("no gh auth: set GH_TOKEN or GITHUB_TOKEN, or run `gh auth login`")

    if problems:
        print(
            "run_bridged: environment validation failed:\n  - " + "\n  - ".join(problems),
            file=sys.stderr,
        )
        raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    # m-TMP (review round 2): tighten umask BEFORE any stdout/stderr
    # write so the redirected log at /tmp/kaizen-bridged-${RUN_ID}.log
    # is created mode 0600 (owner-only) rather than the system default
    # 0644 (world-readable). The log can contain agent-authored prose
    # from `bridge_requests.args_json` if Python ever traces a row on
    # error; making it world-readable would leak that to any local
    # account.
    os.umask(0o077)

    ap = argparse.ArgumentParser(prog="run_bridged")
    ap.add_argument("--db", default=".ai/memex.db", dest="db")
    ap.add_argument("--bridge-db", default=".ai/bridge.db", dest="bridge_db")
    ap.add_argument("--url", required=True, dest="url")
    ap.add_argument("--cycles", type=int, required=True, dest="cycles")
    ap.add_argument("--subject", default=None, dest="subject")
    ap.add_argument("--run-id", type=int, required=True, dest="run_id")
    args = ap.parse_args(argv)

    # 1. Env preflight — BEFORE any clone work.
    validate_environment()

    # 2. Defence in depth: bootstrap the bridge DB even though
    # `skills/improve/SKILL.md` Step 1 should have done so already.
    bootstrap(args.bridge_db)

    # 3. Drive orchestrate_run with the S1-issued run_id and the
    # queue-bridge tools_provider.
    from scripts.cc_tool_bridge import queue_bridge_provider
    from scripts.run import orchestrate_run

    result = orchestrate_run(
        db_path=args.db,
        git_url=args.url,
        cycles_requested=args.cycles,
        subject=args.subject,
        mode="team",
        tools_provider=queue_bridge_provider(args.bridge_db, args.run_id),
        run_id=args.run_id,
    )

    status = result.get("status")
    # Exit zero when the run reaches 'complete'; non-zero for
    # 'failed' so the detached subprocess surfaces failure into the
    # log file S1 tails.
    return 0 if status == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
