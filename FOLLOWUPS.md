# Kaizen Follow-Ups

Small notes captured during multi-PR drops so the work doesn't get lost when
the PR merges. Entries here are intentionally lightweight — the canonical
home for tracked work is GitHub Issues.

## Atelier — agent-teams tmux config + pane-title support

**Captured:** 2026-05-25 (during the F1–F14 + T1–T5 audit drop on kaizen#49)

Kaizen now ships `scripts/_tmux_config.py` (shared agent-teams CONFIG_BLOCK,
marker-versioned install) and `scripts/_tmux_workspace.py` (per-wave
main-vertical layout + `[w{wave_n}] {agent}` pane titles).

**Atelier should adopt the same surface** so a user running `/atelier:run`
gets the same visual structure they get from kaizen-launched cycles:

1. Vendor (or shared-package) the CONFIG_BLOCK + marker helpers — same
   block, same MARKER_VERSION, same `_check_tmux_config` consent flow in
   atelier's installer/setup path. Single source of truth means an update
   in kaizen flows to atelier without manual sync.
2. Add per-wave pane-title support to `atelier/scripts/workspace.py`
   (current home of atelier's workspace orchestration). The signature
   `set_pane_title(workspace, agent, wave_n)` is intentionally identical
   so the integration point is a direct copy.
3. Promote a lead/PM role into tmux's main pane post-spawn — kaizen
   prefers `agent-systems-architect-1` → `software-architect-1` → `pm-1`.
   Atelier's preference order may differ; align with the active roster.

**Out of scope for kaizen#49** because atelier is a separate repo. File a
companion atelier issue (or commit directly to atelier) when picking this
up — kaizen's helpers are not yet packaged for distribution, so the
initial integration may be a copy-paste with a TODO to consolidate later.
