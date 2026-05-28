"""Orchestrator-side tmux workspace fold (kaizen#86).

Run by the **orchestrator session** (the one driving the team-mode bridge poll
loop, whose ``$TMUX`` / ``$TMUX_PANE`` point at the window holding the teammate
panes) to fold that window into "PM-left + 2-column grid":

    python3 -m scripts.fold_workspace [--team-id <id>]

This exists because the in-process layout fold in
``scripts.team_executor`` runs inside the *detached* ``run_bridged`` process,
whose tmux commands never reach the orchestrator's window — so the fold is a
silent no-op and the panes stay a single stacked column. The bridge emits an
``apply_layout`` request (see ``scripts.cc_tool_bridge.QueueBridgeWrapper``);
the orchestrating Claude session services it by invoking THIS module, so the
``select-layout`` / ``join-pane`` calls land on the real window.

``--team-id`` is accepted for logging/symmetry with the bridge request payload;
the fold itself is positional on the live pane list and needs no roster.

Exit code is always 0 (best-effort, like the underlying helper) so a tmux quirk
never fails the orchestrator's bridge write-back.
"""

from __future__ import annotations

import argparse
import sys

from scripts._tmux_workspace import fold_current_window


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.fold_workspace", description=__doc__)
    parser.add_argument(
        "--team-id",
        default="",
        help="CC team id (for log symmetry with the apply_layout bridge payload; "
        "not used to target tmux — the fold operates on the current window).",
    )
    args = parser.parse_args(argv)
    try:
        fold_current_window(workspace_name=args.team_id)
    except Exception as exc:  # pragma: no cover - best-effort, never fail the orchestrator
        print(f"[fold_workspace] fold raised (continuing): {exc!r}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
