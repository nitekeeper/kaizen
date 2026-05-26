"""Post-spawn tmux workspace hooks for kaizen team agent mode.

After kaizen spawns N teammates into a tmux workspace, the panes are
laid out in tmux's default tiled mode and every pane is anonymously
titled ``general-purpose`` (Claude Code's default subagent_type). The
hooks in this module reshape the workspace into:

  - one main pane on the LEFT (the lead architect / PM)
  - a 2-column grid on the RIGHT containing the remaining teammates

and rename each teammate's pane title to ``[w{wave_n}] {agent}`` so a
glance at the tmux window shows which wave each agent is participating in.

# Mapping panes to agents (positional)

Claude Code titles every team-mode pane ``general-purpose``, so there is
no in-band way to tell which pane belongs to which teammate. We rely on
the positional convention: ``tmux list-panes`` returns panes in the
order CC's team_create surfaced them, which mirrors the ``members`` list
passed to ``TeamCreate``. The caller passes ``ordered_agents`` matching
that members list and we build the pane_id → agent map by zip.

If CC ever reorders panes the mapping degrades to "titles point at the
wrong agent," which is visible and easy to spot — better than the prior
silent-no-op behavior.

# 2-column grid (programmatic join-pane)

tmux has no built-in "left main + 2-column right" layout. We build it
in two steps:

  1. ``select-layout main-vertical`` → 1 wide left pane, others stacked
     in a single right column.
  2. ``join-pane -h`` to pair the right-column panes: pane2 joins pane1,
     pane4 joins pane3, etc. Each ``-h`` join makes the source a
     horizontal split of the target, producing a row of 2 panes.

Resulting shape (6 teammates, 1 main + 5 right → odd one left alone):

    ┌──────────┬─────┬─────┐
    │          │  a  │  b  │
    │   main   ├─────┼─────┤
    │  (arch)  │  c  │  d  │
    │          ├─────┴─────┤
    │          │     e     │
    └──────────┴───────────┘

The helpers tolerate "no tmux server running" / "workspace missing"
gracefully: tmux exits non-zero with ``no server running on …`` and
we swallow it (a kaizen run must not fail because the user happens to
not have a tmux server up).
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


def _list_pane_ids(workspace_name: str) -> list[str] | None:
    """Return current pane IDs (positional order) or None on soft failure.

    None is returned when tmux is unavailable OR list-panes hits a hard
    error; the helpers above use None as the "abandon this hook" signal.
    """
    proc = _run_tmux(["list-panes", "-t", workspace_name, "-F", "#{pane_id}"])
    if _tmux_unavailable(proc):
        return None
    if proc.returncode != 0:
        print(
            f"[_tmux_workspace] list-panes for {workspace_name!r} failed: "
            f"{(proc.stderr or '').strip()}",
            file=sys.stderr,
        )
        return None
    return [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]


def apply_workspace_layout(
    workspace_name: str,
    *,
    ordered_agents: list[str],
    main_agent: str | None = None,
) -> dict[str, str]:
    """Apply "left main + right 2-column grid" layout for ``workspace_name``.

    ``ordered_agents`` is the team's member list in the order passed to
    ``TeamCreate``; we map ``pane_ids[i] → ordered_agents[i]`` to recover
    the per-pane identity that CC's ``general-purpose`` titles obscure.

    Returns the pane_id → agent map. The caller passes this dict to
    ``set_pane_titles`` (and re-uses it across waves to retitle without
    re-applying layout).

    No-op (returns ``{}``) when:
      - tmux server isn't running
      - the workspace doesn't exist
      - ``ordered_agents`` is empty

    Best-effort: failures during swap / join surface a single stderr
    warning each and we proceed — a partial layout is better than no
    layout, and a kaizen cycle must not abandon because of a tmux quirk.
    """
    if not ordered_agents:
        return {}

    pane_ids = _list_pane_ids(workspace_name)
    if pane_ids is None or not pane_ids:
        return {}

    # Positional zip — extra panes (e.g. an orchestrator pane CC may
    # interleave in the future) get no agent mapping and are left alone.
    pane_to_agent: dict[str, str] = {
        pane_ids[i]: ordered_agents[i] for i in range(min(len(pane_ids), len(ordered_agents)))
    }

    # Step 1 — main-vertical (1 wide left + N stacked right).
    layout_proc = _run_tmux(["select-layout", "-t", workspace_name, "main-vertical"])
    if _tmux_unavailable(layout_proc):
        return pane_to_agent
    if layout_proc.returncode != 0:
        print(
            f"[_tmux_workspace] select-layout main-vertical for {workspace_name!r} "
            f"failed: {(layout_proc.stderr or '').strip()}",
            file=sys.stderr,
        )
        return pane_to_agent

    # Step 2 — swap main_agent to position 0 if it isn't already there.
    if main_agent:
        target_pane_id: str | None = next(
            (pid for pid, name in pane_to_agent.items() if name == main_agent),
            None,
        )
        if target_pane_id is not None and pane_ids[0] != target_pane_id:
            swap_proc = _run_tmux(["swap-pane", "-t", pane_ids[0], "-s", target_pane_id])
            if swap_proc.returncode != 0 and not _tmux_unavailable(swap_proc):
                print(
                    f"[_tmux_workspace] swap-pane main<-{main_agent} failed: "
                    f"{(swap_proc.stderr or '').strip()}",
                    file=sys.stderr,
                )
            else:
                # Re-list panes so we know the current left-to-right /
                # top-to-bottom order BEFORE we start joining pairs in
                # the right column.
                relisted = _list_pane_ids(workspace_name)
                if relisted is not None and relisted:
                    pane_ids = relisted

    # Step 3 — fold the right column into a 2-column grid. pane_ids[0] is
    # the main pane (left); pane_ids[1:] is the right column from top to
    # bottom. Pair (1+2), (3+4), (5+6), ... by joining the second of each
    # pair into the first via a horizontal split.
    right_panes = pane_ids[1:]
    for i in range(0, len(right_panes) - 1, 2):
        target = right_panes[i]
        source = right_panes[i + 1]
        join_proc = _run_tmux(["join-pane", "-h", "-s", source, "-t", target])
        if join_proc.returncode != 0 and not _tmux_unavailable(join_proc):
            print(
                f"[_tmux_workspace] join-pane -h -s {source} -t {target} failed: "
                f"{(join_proc.stderr or '').strip()}",
                file=sys.stderr,
            )

    return pane_to_agent


def set_pane_titles(workspace_name: str, pane_to_title: dict[str, str]) -> None:
    """Set ``pane_title`` for each ``{pane_id: title}`` in ``pane_to_title``.

    Targets panes by ``pane_id`` (the ``%N`` identifier returned by
    ``list-panes``), so this is robust to layout changes — once we have
    the map from :func:`apply_workspace_layout` we can retitle across
    waves without re-listing panes.

    Tolerant of:
      - tmux server not running (single early return on the first call)
      - individual panes having disappeared (single stderr warning, keep
        going for the rest of the dict)

    ``workspace_name`` is unused at runtime — ``select-pane -t %N`` works
    on the global pane_id — but accepted for symmetry with
    ``apply_workspace_layout`` and for future flexibility (e.g. if we
    ever switch to ``-t {workspace}.{index}`` style targeting).
    """
    del workspace_name  # currently unused; see docstring
    for pane_id, title in pane_to_title.items():
        proc = _run_tmux(["select-pane", "-t", pane_id, "-T", title])
        if proc.returncode == 0:
            continue
        if _tmux_unavailable(proc):
            return
        print(
            f"[_tmux_workspace] select-pane -T {shlex.quote(title)} on {pane_id} failed: "
            f"{(proc.stderr or '').strip()}",
            file=sys.stderr,
        )
