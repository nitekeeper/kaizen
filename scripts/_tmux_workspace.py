"""Post-spawn tmux workspace hooks for kaizen team agent mode.

After kaizen spawns N teammates into a tmux workspace, the panes are
laid out in tmux's default tiled mode and have anonymous titles. The
hooks in this module:

  - apply_main_vertical_layout(workspace, *, main_agent) — switch to
    tmux's main-vertical layout and (optionally) promote a named agent's
    pane to the main (left) pane.
  - set_pane_title(workspace, agent_name, wave_n) — rename the pane
    whose current title contains ``agent_name`` to ``[w{wave_n}] {agent_name}``.

Both helpers tolerate "no tmux server running" gracefully: tmux exits
non-zero with a message like ``no server running on /tmp/tmux-1000/default``
and the helpers swallow it (we don't want a kaizen run to fail because
the user happens to not have a tmux server up).

The hooks are passive — they never spawn panes or windows; they only
adjust existing ones. If the workspace doesn't exist (or no pane matches
the agent name) they log a warning and return early.
"""

from __future__ import annotations

import shlex
import subprocess
import sys

_NO_SERVER_HINTS = (
    "no server running",
    "no current client",
    "can't find session",
    "no such session",
)


def _run_tmux(argv: list[str]) -> subprocess.CompletedProcess:
    """Wrap subprocess.run with tmux-friendly defaults and error tolerance.

    Always uses check=False so the caller can inspect returncode + stderr.
    Captures both streams as text so the no-server signature checks can
    look at stderr.
    """
    return subprocess.run(
        ["tmux", *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _tmux_unavailable(proc: subprocess.CompletedProcess) -> bool:
    """Return True iff ``proc`` indicates "tmux server not running" or similar.

    A nonzero returncode that mentions any of the no-server hints in
    stderr/stdout is treated as a soft failure. The caller returns early
    without raising.
    """
    if proc.returncode == 0:
        return False
    blob = (proc.stderr or "") + (proc.stdout or "")
    blob_lower = blob.lower()
    return any(hint in blob_lower for hint in _NO_SERVER_HINTS)


def apply_main_vertical_layout(workspace_name: str, *, main_agent: str) -> None:
    """Switch ``workspace_name`` to main-vertical layout and promote ``main_agent``.

    Lists panes, asks tmux to select-layout main-vertical, then (if
    ``main_agent`` is provided and matches a pane title) swap-panes so
    that pane becomes the main (left) one.

    No-op when:
      - tmux server isn't running
      - the workspace doesn't exist
      - main_agent doesn't match any pane title
    """
    list_proc = _run_tmux(["list-panes", "-t", workspace_name, "-F", "#{pane_id} #{pane_title}"])
    if _tmux_unavailable(list_proc):
        return
    if list_proc.returncode != 0:
        # Unknown workspace or other tmux error — soft-fail with a single
        # warning so kaizen continues regardless.
        print(
            f"[_tmux_workspace] list-panes for {workspace_name!r} failed: "
            f"{(list_proc.stderr or '').strip()}",
            file=sys.stderr,
        )
        return

    layout_proc = _run_tmux(["select-layout", "-t", workspace_name, "main-vertical"])
    if _tmux_unavailable(layout_proc):
        return
    if layout_proc.returncode != 0:
        print(
            f"[_tmux_workspace] select-layout main-vertical for "
            f"{workspace_name!r} failed: {(layout_proc.stderr or '').strip()}",
            file=sys.stderr,
        )
        return

    if not main_agent:
        return

    target_pane_id: str | None = None
    main_pane_id: str | None = None
    for line in (list_proc.stdout or "").splitlines():
        parts = line.split(" ", 1)
        if len(parts) != 2:
            continue
        pane_id, title = parts[0], parts[1]
        if main_pane_id is None:
            # main-vertical puts the first listed pane as the main pane.
            main_pane_id = pane_id
        if main_agent in title and target_pane_id is None:
            target_pane_id = pane_id

    if target_pane_id is None or main_pane_id is None or target_pane_id == main_pane_id:
        return

    swap_proc = _run_tmux(["swap-pane", "-t", main_pane_id, "-s", target_pane_id])
    if swap_proc.returncode != 0 and not _tmux_unavailable(swap_proc):
        print(
            f"[_tmux_workspace] swap-pane main<-{main_agent} failed: "
            f"{(swap_proc.stderr or '').strip()}",
            file=sys.stderr,
        )


def set_pane_title(workspace_name: str, agent_name: str, wave_n: int) -> None:
    """Rename the pane currently showing ``agent_name`` to ``[w{wave_n}] {agent_name}``.

    Finds the pane via ``list-panes`` substring match on the existing
    title. Soft-fails (single stderr warning) when:
      - tmux server isn't running
      - the workspace doesn't exist
      - no pane title contains ``agent_name``
    """
    list_proc = _run_tmux(["list-panes", "-t", workspace_name, "-F", "#{pane_id} #{pane_title}"])
    if _tmux_unavailable(list_proc):
        return
    if list_proc.returncode != 0:
        print(
            f"[_tmux_workspace] list-panes for {workspace_name!r} failed: "
            f"{(list_proc.stderr or '').strip()}",
            file=sys.stderr,
        )
        return

    pane_id: str | None = None
    for line in (list_proc.stdout or "").splitlines():
        parts = line.split(" ", 1)
        if len(parts) != 2:
            continue
        pid, title = parts[0], parts[1]
        if agent_name in title:
            pane_id = pid
            break
    if pane_id is None:
        print(
            f"[_tmux_workspace] no pane in {workspace_name!r} matches "
            f"agent {agent_name!r}; skipping title update.",
            file=sys.stderr,
        )
        return

    new_title = f"[w{wave_n}] {agent_name}"
    title_proc = _run_tmux(["select-pane", "-t", pane_id, "-T", new_title])
    if title_proc.returncode != 0 and not _tmux_unavailable(title_proc):
        print(
            f"[_tmux_workspace] select-pane -T {shlex.quote(new_title)} failed: "
            f"{(title_proc.stderr or '').strip()}",
            file=sys.stderr,
        )
