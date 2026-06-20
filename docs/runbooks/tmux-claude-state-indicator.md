# tmux Claude state indicator

Optional integration of the third-party `accessd/tmux-agent-indicator` plugin so kaizen team-mode panes show a richer running / needs-input / done indicator, without kaizen ever installing or writing global config.

## What it is

[`accessd/tmux-agent-indicator`](https://github.com/accessd/tmux-agent-indicator) (MIT, active, default branch `main`) is a tmux plugin that surfaces a three-state agent indicator per pane:

| State | Meaning | How it shows |
|---|---|---|
| **running** | the agent is working | pane border color + a Knight Rider animation; status-bar icon |
| **needs-input** | the agent is waiting on you (e.g. a permission prompt) | `needs-input` border (default `yellow`); window-title styling; status-bar icon |
| **done** | the agent finished its turn | `done` border (default `green`); `done` window-title bg (default `red`); status-bar icon |

States reset on pane focus or on the next transition. It also offers an optional pane background tint (OFF by default), a per-agent status-bar icon, and a multi-session "session dots" attention row. Requirements: **tmux 3.0+ and bash 4+**. It supports Claude Code (via hooks), Codex (notify), and OpenCode (plugin); kaizen's scope is **Claude Code only**.

## Install (operator step)

**Kaizen does NOT install this plugin and does NOT write your `~/.claude/settings.json` or `~/.tmux.conf`.** You install it yourself; the installer wires the Claude Code hooks. Two upstream-supported paths:

One-command (README-recommended):

```
curl -fsSL https://raw.githubusercontent.com/accessd/tmux-agent-indicator/main/install.sh | bash
```

This installs to `~/.tmux/plugins/tmux-agent-indicator` and wires the Claude Code hooks into `~/.claude/settings.json` (it also touches `~/.codex/config.toml` and `~/.config/opencode/plugins/`, which are irrelevant to kaizen).

Or via TPM:

```
set -g @plugin 'accessd/tmux-agent-indicator'
tmux source-file ~/.tmux.conf
```

then install the plugin through TPM as you normally do.

To actually SEE the indicator, the plugin needs a status-bar placeholder, e.g. `set -g status-right '#{agent_indicator} | %H:%M'` (kaizen adds exactly this inside its own block when it detects the plugin — see below).

The Claude Code hooks the installer wires map agent lifecycle events to states:

| Hook event | State |
|---|---|
| `UserPromptSubmit` | resets (`--state off`) then sets `running` |
| `PermissionRequest` | `needs-input` |
| `Stop` | `done` |

(The hook event is `PermissionRequest`, not `Notification`.) The full hook JSON lives in the plugin's `hooks/claude-hooks.json` — refer to the upstream repo rather than this runbook for the exact template.

## How kaizen integrates it

Integration model: **detect-and-source** (operator-consent-respecting). Kaizen's generated tmux config block (`scripts/_tmux_config.py`, `CONFIG_BLOCK`) carries an `if-shell -b` guard:

```
if-shell -b '[ -d "$HOME/.tmux/plugins/tmux-agent-indicator" ]' " \
    source-file -q '$HOME/.tmux/plugins/tmux-agent-indicator/agent-indicator.tmux' ; \
    set -g @agent-indicator-icons 'claude=🤖,codex=🧠,opencode=💻,default=🤖' ; \
    set -g @agent-indicator-indicator-enabled 'on' ; \
    set -g status-right '#{agent_indicator} | %H:%M' \
"
```

- The guard is re-evaluated at config **load** time, so it works even if you install the plugin AFTER kaizen wrote the block.
- When the plugin dir is present, kaizen sources the plugin bootstrap (`-q`, so a missing/renamed file never errors), pins the icon map (the Claude icon is the `claude=` entry inside the single `@agent-indicator-icons` option — there is no standalone `@agent-indicator-icon-claude`), enables the indicator, and adds the `#{agent_indicator}` placeholder to `status-right`.
- When the plugin dir is absent, the whole branch is a harmless no-op.

The kaizen#76 composite `pane-border-format` render (CC's OSC 2 activity glyph + the `@desired_title` wave/role label) is set **unconditionally, outside the guard**. It is the zero-dependency fallback when the plugin is absent, and it keeps carrying the wave/role label even when the plugin is present (the plugin styles window-scoped border *styles* and the status bar; it does not own `pane-border-format`).

Kaizen never runs `curl`, never runs `install.sh`, and never mutates `~/.claude/settings.json` or `~/.tmux.conf`. The integration is purely additive tmux directives inside kaizen's own marker-wrapped block.

## allow-passthrough is NOT required

Issue #79's original Tier-A premise assumed this plugin needs `allow-passthrough` on. **That is wrong.** The plugin has zero occurrences of `allow-passthrough` anywhere in its source. It drives state via Claude Code **hooks** plus `tmux set-option` / `tmux set-hook` — not terminal escape passthrough. Do **not** set `allow-passthrough` for this; kaizen does not, and you should not need to either.

## Trade-off: composite render vs the plugin

| | kaizen composite render (kaizen#76, fallback) | tmux-agent-indicator plugin |
|---|---|---|
| States | 2 (idle / busy, via the OSC 2 glyph in `pane_title`) | 3 (running / needs-input / done) |
| Signal source | CC's OSC 2 pane-title glyph + kaizen's `@desired_title` label | CC's official hooks → `agent-state.sh` |
| Wave/role label | yes (`@desired_title`) | no (kaizen keeps it via the unconditional border render) |
| Dependencies | none — built into kaizen's block | operator must install the plugin + its hooks |
| Border scope | per-pane via `pane-border-format` | window-scoped border *styles* (see caveat) |

**Border caveat (verbatim upstream):** tmux border coloring is window-scoped (`pane-active-border-style` / `pane-border-style`); tmux cannot set a fully independent border color for one arbitrary NON-active pane. The plugin works within that constraint.

**Coexistence is validated here only at the config-composition level** — kaizen's `pane-border-format` and the plugin's status-bar/border-style integration compose by *different* mechanisms, so they are plausibly compatible. Upstream does not document coexistence with an external `pane-border-format` owner, so the live combined render is **NEEDS-TESTING**: smoke-test it on your machine after installing the plugin before relying on it in a real run.

> **Note (M8c-2).** The run-76 team-window grid-fold machinery (`scripts/_tmux_workspace.py`, the `after-split-window[88]` reconcile hook, and the pane-fold reconcile mechanisms) was removed with the rest of the `--mode team` subsystem. Only the activity-glyph / pane-border `CONFIG_BLOCK` documented above remains.

## Related

- `scripts/_tmux_config.py` — `CONFIG_BLOCK`, where the detect-and-source guard and the activity-glyph readiness helpers live.
- kaizen#76 (dual-signal composite render) and kaizen#64 (the `@desired_title` → border mechanism this builds on).
- [`accessd/tmux-agent-indicator`](https://github.com/accessd/tmux-agent-indicator) — upstream plugin (MIT).
