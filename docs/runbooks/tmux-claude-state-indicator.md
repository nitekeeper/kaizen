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

## Team-window grid invariant (run-76 layout consistency)

The panes this indicator styles are kept in a pinned shape. The invariant (encoded as a testable predicate — `expected_grid_geometry` / `grid_invariant_check` in `scripts/_tmux_workspace.py`):

> After a `grid-2col` fold settles, the PM/orchestrator pane sits strictly LEFT of every teammate pane, and the teammate panes form a 2-column grid read top-to-bottom. An ODD teammate count leaves one trailing **banner row** — a single full-width pane at the bottom of the right area (e.g. 4 panes → rows `(2, 2)`; 5 panes → `(2, 2, 1)`).

`KAIZEN_TEAMMATE_LAYOUT=stripes` opts out of the grid, and the default (`auto`) resolves to `stripes` for ≤3 teammate panes and `grid-2col` for ≥4 — the invariant (and its verification) only exists in `grid-2col` mode.

### The three mechanisms that maintain it

In precedence order — earlier means it reacts sooner; all three converge on the same idempotent reset-then-fold reconcile (`scripts/_tmux_workspace.py:fold_current_window`):

1. **`after-split-window[88]` hook reconcile (primary, immediate).** Installed once at workspace boot (`scripts/_tmux_config.py:install_team_window_hook`, called from `scripts/team_executor.py` only when the pane map is non-empty — positive evidence the executor's tmux context reaches the team window). tmux's own pane-ADD event triggers the fold, closing the materialize-vs-fold race at the source. Self-gated per window on the `@kaizen_team_id` user-option; the comparison happens in tmux's FORMAT layer (`#{==:...}` expands to a literal `0`/`1` before `/bin/sh` runs), so no option value ever reaches the shell and the hook is a zero-side-effect no-op on your other windows. A `@kaizen_fold_hook_running` window option guards against re-entrancy. A refused install (no `$TMUX`/`$TMUX_PANE`, allowlist failure) is log-and-continue — the run proceeds on mechanisms 2+3. Teardown runs in the cycle's `finally` block on EVERY exit path (success, abandonment, exception); `set-hook -gu 'after-split-window[88]'` removes only kaizen's array entry.
2. **Pane-signature delta trigger (mid-phase).** `scripts/team_executor.py:_flag_refold_on_pane_delta` compares the live teammate pane SET against the signature captured at the last fold request, at each message-servicing opportunity. ANY membership change — add, remove, or respawn id-churn — flags a re-fold, coalesced to ONE fold per batch (never one per message).
3. **Phase-boundary fold (backstop).** `scripts/team_executor.py:_phase_boundary_fold` — one unconditional idempotent fold per Phase-4 wave boundary and per Phase-5b' reviewer iteration. Demoted from primary to backstop by run-76, but deliberately KEPT: it self-heals the grid at a coarser cadence whenever the hook was refused or a delta slipped past mechanism 2.

### Fold-until-stable + geometry verification

Each reconcile loops read-then-fold until two consecutive pane-set reads agree, bounded by `KAIZEN_FOLD_STABLE_MAX_ITERS` (env; default `5`). The floor is `2` — one iteration folds, a second confirms stability — so values below 2 are clamped to 2 with a stderr warning, and a non-integer value falls back to the default with a warning. Once the set quiesces (grid-2col only), the observed geometry is checked against the expected per-row pane counts (including the odd-count banner row); on a definite mismatch there is at most ONE extra reset-then-fold retry, then a single loud give-up warning. Geometry is cosmetic — verification degrades, it never aborts a cycle.

### Diagnosis — what to grep for

All warnings go to stderr (`[_tmux_workspace]` / `[_tmux_config]` prefixes) or the executor log:

| Substring | Meaning |
|---|---|
| `fold_current_window no-op` | Nothing to fold: tmux unavailable / no teammate panes in the window, or `select-layout` did not apply. Layout left unchanged. |
| `pane set still changing` | Settle cap (`KAIZEN_FOLD_STABLE_MAX_ITERS`) exhausted while the pane set was still churning; the grid may be transiently collapsed. Recovery: the delta trigger / boundary fold re-runs the whole reconcile once the set settles. |
| `fold geometry unmet` | The pane set quiesced but the geometry still violated the invariant after the single retry; layout left as-is (best-effort). |
| `is below the floor of` | `KAIZEN_FOLD_STABLE_MAX_ITERS` clamp warning (value < 2). |
| `is not an integer` | `KAIZEN_FOLD_STABLE_MAX_ITERS` parse-failure fallback warning. |
| `install_team_window_hook skipped` / `reconcile hook not installed` | Hook install refused (missing tmux env, allowlist failure, or a failed tmux write — window tag or `set-hook`); the run continues on the delta trigger + boundary fold. |
| `remove_team_window_hook: set-hook -gu` | Hook teardown at cycle end failed — the `set-hook -gu` unset itself errored, so the hook may still be live in your tmux server. Fix by hand: `tmux set-hook -gu 'after-split-window[88]'`. |

### Limitations (deliberate)

- **The whole window is the fold's work area — don't cohabit it.** The fold path has no roster filter: `_list_pane_ids` returns every pane in the current window minus only the orchestrator's own pane (`TMUX_PANE`). A pane you split into the kaizen window therefore (a) fires the reconcile hook itself, (b) is folded INTO the grid — `join-pane` relocates and resizes it like any teammate pane — and (c) is counted by the geometry verification, so the verdict stays `True` even though your pane has been moved. Keep operator panes in a separate window. (Transient edge cases: a tracked pane VANISHING between the fold's pane-set read and the geometry read makes the verdict `None` — unverifiable, check skipped; a pane APPEARING between the reads is simply absent from the checked set and stays silently uncounted — verification can still return `True` without flagging it.)
- **Index 88 is server-global; no concurrent runs.** The hook is installed at the fixed server-global array index `after-split-window[88]`, so two concurrent kaizen runs on one tmux server would collide on it. Concurrent multi-repo kaizen runs are already barred operationally, so a single well-known index is safe — and it is what makes teardown surgical (only kaizen's entry is removed).

## Related

- `scripts/_tmux_config.py` — `CONFIG_BLOCK`, where the detect-and-source guard lives; also the run-76 reconcile-hook helpers (`build_team_fold_hook_command`, `install_team_window_hook`, `remove_team_window_hook`).
- `scripts/_tmux_workspace.py` — the grid-invariant predicate (`expected_grid_geometry`, `grid_invariant_check`) and the fold-until-stable reconcile (`fold_current_window`).
- kaizen#76 (dual-signal composite render) and kaizen#64 (the `@desired_title` → border mechanism this builds on).
- [`accessd/tmux-agent-indicator`](https://github.com/accessd/tmux-agent-indicator) — upstream plugin (MIT).
- `docs/runbooks/orphan-teammate-cleanup.md` — the pane/process/config "trifecta" for cleaning up team-mode panes (the same panes this indicator styles).
