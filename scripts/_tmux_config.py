"""Shared agent-teams tmux config block + setup helpers.

Kaizen and other agent-teams-based tooling share a small block of tmux
configuration so per-pane titles and main-vertical layouts render
consistently across workspaces. This module defines:

  - CONFIG_BLOCK         : the canonical tmux directives to install
  - MARKER_VERSION       : monotonically-increasing int; bump when CONFIG_BLOCK
                          changes so existing installs can detect "an update
                          is available" without diffing prose
  - MARKER_START / END   : sentinel comments that wrap the block so we can
                          detect, replace, or remove it safely
  - detect_existing_marker(path) -> int | None
  - apply_config_block(path, version) -> None
  - show_diff(path, version) -> str

`scripts/setup.py` consumes these helpers from a consent flow that asks
the user before writing.

Idempotency contract: calling apply_config_block twice in a row at the
same version is a no-op the second time (the marker is detected and the
block is left in place). Bumping MARKER_VERSION requires updating
CONFIG_BLOCK in the same change; the detector then surfaces "v{old} →
v{new} available" to the user.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# MARKER_VERSION is the installed-config schema, NOT the tmux binary version.
MARKER_VERSION = 4
MARKER_START = "# >>> agent-teams v{} >>>"
MARKER_END = "# <<< agent-teams v{} <<<"

# Verbatim from the spec — keep this in sync with any future MARKER_VERSION bumps.
#
# kaizen#64 (v2): the pane border now renders ``#{@desired_title}`` with a
# fallback to ``#{pane_title}``. Background: when a Claude Code team-mode
# pane becomes active, the subagent process emits an OSC 2 escape sequence
# (``ESC ] 2 ; general-purpose BEL``) which tmux honors unconditionally,
# overwriting the ``pane_title`` we set via ``select-pane -T``. tmux 3.4
# has no gate to disable OSC 2 → pane_title (``allow-rename`` only gates
# the legacy escape-k window-rename). We sidestep the override by storing
# the authoritative title in the ``@desired_title`` per-pane user-option
# (which OSC 2 cannot touch) and rendering THAT in the border. ``pane_title``
# may still flicker to ``general-purpose`` in tmux's internal state, but
# the operator-visible border keeps the wave/role label.
#
# kaizen#76 (v3): the v2 render hid CC's OSC 2 activity glyph (the leading
# ``*`` / Braille spinner char that signals idle vs busy) because the
# format-string OVERRODE ``pane_title`` with ``@desired_title`` whenever the
# latter was set. The dual-signal fix: COMPOSE both channels in
# ``pane-border-format`` — ``#{=1:pane_title}`` exposes the first display
# column of ``pane_title`` (CC's activity glyph slot) and the existing
# conditional renders ``@desired_title`` (or ``pane_title`` fallback) for
# the kaizen-owned label. Run 40 was aborted before this design landed
# because the prior plan (``set -g allow-set-title off``) would have
# silenced the glyph stream entirely — a regression the operator caught
# from a single screen observation. See ``feedback-tmux-pane-title-dual-signal``
# for the memory entry that records this lesson.
#
# Known cosmetic artifacts (deliberate trade-offs, NOT bugs):
#   * Doubled glyph during the ~50ms un-tagged-pane init window — when
#     ``@desired_title`` is unset the format falls through to
#     ``#{=1:pane_title} #{pane_title}`` which double-prints the leading
#     glyph. Below the perceptual-flash threshold; do NOT add an
#     ``#{?#{m:*general-purpose*,...},...,...}`` conditional to "fix" it
#     because that re-couples the render to a CC-internal string.
#   * Bare ``g`` first char when ``pane_title`` is literally
#     ``general-purpose`` (CC idle with no OSC 2 emission). Same rationale
#     as above — acceptable cosmetic vs CC-internals coupling.
#
# kaizen#79 (v4): OPTIONAL integration with the third-party
# ``accessd/tmux-agent-indicator`` plugin (MIT; tmux 3.0+, bash 4+). That
# plugin drives a richer THREE-state indicator (running / needs-input /
# done) off Claude Code's official hooks + ``tmux set-option``/``set-hook``
# — NOT terminal escape passthrough (it does NOT use or require
# ``allow-passthrough``). The integration model is DETECT-AND-SOURCE:
#
#   * The composite ``pane-border-format`` render above is set
#     UNCONDITIONALLY and remains the zero-dependency FALLBACK — it carries
#     the wave/role label and CC's idle/busy glyph whether or not the plugin
#     is installed.
#   * An ``if-shell -b`` guard re-evaluates plugin presence at config LOAD
#     time (robust if the operator installs the plugin AFTER kaizen wrote
#     the block). When ``~/.tmux/plugins/tmux-agent-indicator`` exists, we
#     source the plugin's bootstrap, add the ``#{agent_indicator}``
#     placeholder to ``status-right`` (composed with the time, not
#     clobbering an operator's own status-right which lives outside this
#     marker block), and pin the icon map so the Claude icon is the default
#     🤖. The ``claude=`` icon is the entry inside the single
#     ``@agent-indicator-icons`` option — there is no standalone
#     ``@agent-indicator-icon-claude``.
#
# Kaizen NEVER installs the plugin and NEVER writes ``~/.claude/settings.json``
# or ``~/.tmux.conf`` directly — the operator runs the upstream installer
# (which wires the CC hooks). See ``docs/runbooks/tmux-claude-state-indicator.md``.
CONFIG_BLOCK = """# Pane border format — compose CC's OSC 2 activity glyph (#{=1:pane_title})
# with kaizen's @desired_title (with pane_title fallback for un-tagged panes).
# kaizen#76 — dual-signal: the leading char of pane_title carries CC's idle/busy
# indicator; @desired_title carries the wave/role label and is immune to OSC 2.
# This render is UNCONDITIONAL — it is the zero-dependency fallback (kaizen#79).
set -g pane-border-status top
set -g pane-border-format '#{=1:pane_title} #[fg=cyan]#{?@desired_title,#{@desired_title},#{pane_title}}#[default]'

# Default layout for new windows: main-vertical
# Apps may override per-window via select-layout.
set -g main-pane-width 60

# kaizen#79 — OPTIONAL detect-and-source integration of the third-party
# accessd/tmux-agent-indicator plugin (3-state running/needs-input/done off
# CC hooks). Guarded by if-shell -b so detection re-runs at config LOAD time;
# a no-op when the plugin dir is absent. Kaizen does NOT install the plugin;
# the plugin drives state via CC hooks (no terminal escape passthrough needed,
# see docs/runbooks/tmux-claude-state-indicator.md).
if-shell -b '[ -d "$HOME/.tmux/plugins/tmux-agent-indicator" ]' " \\
    source-file -q '$HOME/.tmux/plugins/tmux-agent-indicator/agent-indicator.tmux' ; \\
    set -g @agent-indicator-icons 'claude=🤖,codex=🧠,opencode=💻,default=🤖' ; \\
    set -g @agent-indicator-indicator-enabled 'on' ; \\
    set -g status-right '#{agent_indicator} | %H:%M' \\
"
"""


def _full_block(version: int) -> str:
    """Return the marker-wrapped block at ``version``.

    Has a trailing newline so the caller can append-with-blank-line without
    extra book-keeping.
    """
    return f"{MARKER_START.format(version)}\n{CONFIG_BLOCK}{MARKER_END.format(version)}\n"


def _read_text(path: Path) -> str:
    """Read ``path`` as UTF-8, returning '' for missing files.

    OS-level errors other than FileNotFound propagate so a permission
    issue surfaces loudly instead of silently treating the file as empty.
    """
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def detect_existing_marker(tmux_conf_path: Path) -> int | None:
    """Scan ``tmux_conf_path`` for an agent-teams marker and return its version.

    Returns:
        - the integer version of the START marker if present and well-formed
        - None if the file is missing OR contains no marker

    Raises:
        ValueError when a marker is present but malformed (e.g. the version
        portion is non-numeric, or START and END markers disagree).
    """
    text = _read_text(tmux_conf_path)
    if not text:
        return None
    start_versions: list[int] = []
    end_versions: list[int] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        # Recognize both START and END marker shapes; same parsing logic.
        for prefix, bucket in (
            ("# >>> agent-teams v", start_versions),
            ("# <<< agent-teams v", end_versions),
        ):
            if line.startswith(prefix):
                suffix = line[len(prefix) :]
                # Extract the integer portion up to the next space.
                num_str = suffix.split()[0] if suffix else ""
                try:
                    bucket.append(int(num_str))
                except ValueError as exc:
                    raise ValueError(
                        f"Malformed agent-teams marker in {tmux_conf_path}: "
                        f"could not parse version from line {raw_line!r}"
                    ) from exc
    if not start_versions and not end_versions:
        return None
    if not start_versions or not end_versions:
        raise ValueError(
            f"Malformed agent-teams marker in {tmux_conf_path}: "
            f"START and END counts disagree ({len(start_versions)} starts, "
            f"{len(end_versions)} ends)"
        )
    if start_versions[0] != end_versions[0]:
        raise ValueError(
            f"Malformed agent-teams marker in {tmux_conf_path}: "
            f"START version {start_versions[0]} != END version {end_versions[0]}"
        )
    return start_versions[0]


def _strip_existing_block(text: str) -> str:
    """Remove any agent-teams block (and its enclosing blank lines) from ``text``.

    Tolerant of multiple-blank-line padding. Used by apply_config_block when
    replacing an older version.
    """
    lines = text.splitlines(keepends=False)
    out: list[str] = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        if not skipping and stripped.startswith("# >>> agent-teams v"):
            # Pop trailing blank lines from `out` so we don't leave a hole.
            while out and out[-1].strip() == "":
                out.pop()
            skipping = True
            continue
        if skipping:
            if stripped.startswith("# <<< agent-teams v"):
                skipping = False
            continue
        out.append(line)
    # Re-join with the file's likely trailing newline; if original had no
    # trailing newline we keep it that way.
    joined = "\n".join(out)
    if text.endswith("\n") and not joined.endswith("\n"):
        joined += "\n"
    return joined


def _safe_write(path: Path, content: str, *, backup: bool) -> None:
    """Atomically write ``content`` to ``path``, writing THROUGH symlinks.

    When ``path`` exists, the write targets ``path.resolve()`` so a symlinked
    ``~/.tmux.conf`` stays a symlink and the REAL file receives the content —
    a naive ``os.replace(tmp, path)`` would clobber the symlink itself with a
    regular file.

    Backup semantics: when ``backup`` is True and the target already exists,
    the target's PRIOR state (the bytes in place before this write) is copied
    to ``<name>.kaizen.bak`` beside the target first. The .bak therefore
    always holds the pre-write content of the most recent write — on an
    idempotent re-apply it simply equals the (unchanged) current content.

    The write itself goes to a temp file beside the target (suffix
    ``.kaizen.tmp<pid>``) followed by ``os.replace`` so a crash mid-write can
    never leave a truncated conf.
    """
    # is_symlink() first: a dangling symlink reports exists() == False, but
    # os.replace on the un-resolved path would clobber the symlink itself.
    target = path.resolve() if (path.is_symlink() or path.exists()) else path
    target.parent.mkdir(parents=True, exist_ok=True)
    if backup and target.exists():
        shutil.copy2(target, target.with_name(target.name + ".kaizen.bak"))
    tmp = target.with_name(f"{target.name}.kaizen.tmp{os.getpid()}")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def apply_config_block(tmux_conf_path: Path, version: int) -> None:
    """Install or replace the agent-teams block in ``tmux_conf_path``.

    If the file does not exist, create it containing only the block.
    If the file exists but has no marker, append the block (with one
    blank line separating it from existing content).
    If the file already has a marker, REPLACE it in place with the
    current version block.

    Idempotent: calling twice at the same version with no edits between
    leaves the file byte-identical the second time.

    Writes go through :func:`_safe_write` (atomic, symlink-preserving). The
    replace/append branches first back up the PRIOR file state to
    ``<name>.kaizen.bak`` beside the (resolved) target; the create branch has
    no prior state, so no backup is made.
    """
    existing = _read_text(tmux_conf_path)
    block = _full_block(version)
    if not existing:
        _safe_write(tmux_conf_path, block, backup=False)
        return
    if detect_existing_marker(tmux_conf_path) is not None:
        # Replace in place.
        stripped = _strip_existing_block(existing)
        # If stripping left content, separate it from the new block with one
        # blank line so the file remains readable.
        sep = ""
        if stripped and not stripped.endswith("\n"):
            sep = "\n\n"
        elif stripped:
            sep = "\n"
        _safe_write(tmux_conf_path, stripped + sep + block, backup=True)
        return
    # No marker — append with one blank line separator.
    sep = ""
    if not existing.endswith("\n"):
        sep = "\n\n"
    elif not existing.endswith("\n\n"):
        sep = "\n"
    _safe_write(tmux_conf_path, existing + sep + block, backup=True)


def show_diff(tmux_conf_path: Path, version: int) -> str:
    """Return a human-readable diff string describing what ``apply_config_block`` would do.

    Format is intentionally lightweight — this is for an interactive prompt,
    not a CI diff parser. Three modes:
      - file missing: returns a "will create" preview
      - file exists, no marker: returns a "will append" preview
      - file exists, marker present: returns the v{old} → v{new} replacement
    """
    if not tmux_conf_path.exists():
        return (
            f"(create) {tmux_conf_path} ← new file with v{version} block:\n\n{_full_block(version)}"
        )
    existing_version = detect_existing_marker(tmux_conf_path)
    if existing_version is None:
        return f"(append) {tmux_conf_path}:\n\n{_full_block(version)}"
    if existing_version == version:
        return f"(no-op) {tmux_conf_path} already at v{version}"
    return (
        f"(update) {tmux_conf_path}: replace v{existing_version} with v{version}:\n\n"
        f"{_full_block(version)}"
    )


# ── kaizen#98: activity-glyph readiness helpers ────────────────────────────
#
# Claude Code's team-mode panes carry an OSC 2 activity glyph (the leading
# braille spinner / idle dot in pane_title) that the v4 CONFIG_BLOCK composes
# into the pane border. tmux's DEFAULT ``allow-set-title`` is ``on``, so a
# fresh v4 install renders the glyph. The bug class (kaizen#98) is OPERATOR
# CONFIG DRIFT: a machine stuck on the old v2 block that set
# ``allow-set-title off`` (which gates the OSC 2 stream) renders no glyph.
#
# ``allow-set-title off`` is the CONFIRMED glyph-gating directive — it is the
# per-pane tmux option that gates the OSC 2 escape carrying CC's glyph.
# ``set-titles off`` gates only the OUTER terminal title (not pane_title) and
# ``allow-passthrough`` gates DCS passthrough (the v4 agent-indicator path
# explicitly does NOT use it — see CONFIG_BLOCK kaizen#79 note), so neither
# is included here; adding them would emit false-positive warnings.
_GLYPH_GATING_DIRECTIVES = ("allow-set-title off",)

# Matches a tmux ``set`` family command and captures the option + value, e.g.
# ``set -g allow-set-title off`` / ``setw -gq allow-set-title off`` /
# ``set-option -g allow-set-title off``. String-based + tmux-free by design.
# The option group requires a leading letter so a flag cluster (``-gu``) can
# never be misparsed as the option name (kaizen#98 review NIT) — option names
# always start with a letter, flags always start with ``-``.
_SET_CMD_RE = re.compile(
    r"^\s*set(?:w|-option|-window-option)?"  # set / setw / set-option / set-window-option
    r"(?:\s+-[A-Za-z]+)*"  # optional flags: -g, -p, -gq, ...
    r"\s+(?P<opt>[A-Za-z][\w-]*)"  # option name (letter-led; may contain hyphens)
    r"\s+(?P<val>\S+)"  # value
)


def _unquote(value: str) -> str:
    """Strip a single pair of matching surrounding quotes from a tmux value.

    tmux accepts ``set -g allow-set-title 'off'`` / ``"off"`` as well as the
    bare ``off``; the regex captures the quotes into the value, so normalize
    them away before comparison (kaizen#98 review NIT).
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _block_sets_option_to(block_text: str, option: str, value: str) -> bool:
    """True if any ``set`` line in ``block_text`` sets ``option`` to ``value``.

    Case-insensitive on the value (``off`` vs ``Off``); tmux option names are
    case-sensitive so ``option`` is matched exactly. Pure string scan — no
    tmux invocation.
    """
    for raw in block_text.splitlines():
        m = _SET_CMD_RE.match(raw)
        if m is None:
            continue
        if m.group("opt") == option and _unquote(m.group("val")).lower() == value.lower():
            return True
    return False


def extract_installed_block(tmux_conf_path: Path) -> str:
    """Return the body text BETWEEN the agent-teams markers, or '' if none.

    Excludes the START/END marker lines themselves. Tolerant of a missing
    file (returns ''). Used by setup.py to diff the OLD installed block
    against the new ``CONFIG_BLOCK`` for glyph-gating-directive removal.
    """
    text = _read_text(tmux_conf_path)
    if not text:
        return ""
    body: list[str] = []
    inside = False
    for line in text.splitlines():
        stripped = line.strip()
        if not inside and stripped.startswith("# >>> agent-teams v"):
            inside = True
            continue
        if inside and stripped.startswith("# <<< agent-teams v"):
            break
        if inside:
            body.append(line)
    return "\n".join(body)


def removed_glyph_gating_directives(old_block: str, new_block: str) -> list[str]:
    """Return glyph-gating directives present in ``old_block`` but not ``new_block``.

    Pure, tmux-free, unit-testable. ``old_block`` / ``new_block`` are the
    agent-teams block bodies (marker-stripped). The confirmed landmine is
    ``allow-set-title off`` — a v2→v4 in-place upgrade removes it from the
    FILE, but a RUNNING tmux server keeps the option set until restart
    (``source-file`` does not unset a removed option). Returning the removed
    directives lets the caller warn that the live session is still gated.
    """
    removed: list[str] = []
    for directive in _GLYPH_GATING_DIRECTIVES:
        option, _, value = directive.partition(" ")
        if _block_sets_option_to(old_block, option, value) and not _block_sets_option_to(
            new_block, option, value
        ):
            removed.append(directive)
    return removed


def check_glyph_readiness(
    tmux_conf_path: Path,
    *,
    live_allow_set_title: str | None = None,
) -> list[str]:
    """Return actionable warnings about activity-glyph readiness; ``[]`` when fresh.

    Advisory only — the caller logs these and NEVER fails on them. Two checks:

      1. Stale marker — the conf's agent-teams marker version is
         ``< MARKER_VERSION`` (the composite glyph render may be missing or
         glyph-less on the older block).
      2. Glyph blocked — ``allow-set-title off`` is in effect, either as a
         directive in the conf file and/or as the passed-in live runtime value
         (``live_allow_set_title == "off"``). When a server is present the
         caller can read the live value via ``tmux show-options`` and pass it;
         otherwise pass ``None`` and rely on the file check.

    Tolerant of a missing conf file (the file checks simply find nothing). A
    malformed marker is surfaced as a single warning rather than raised.
    """
    path = Path(tmux_conf_path)
    warnings: list[str] = []
    try:
        version = detect_existing_marker(path)
    except ValueError:
        return [
            f"tmux activity-glyph config in {path} has a malformed agent-teams "
            f"marker; run setup.py to repair it so the live Claude idle/busy "
            f"glyph renders."
        ]
    if version is not None and version < MARKER_VERSION:
        warnings.append(
            f"tmux activity-glyph config is v{version}; run setup.py to upgrade "
            f"to v{MARKER_VERSION} (the live Claude idle/busy glyph may not "
            f"render on the stale block)."
        )
    text = _read_text(path)
    file_gated = _block_sets_option_to(text, "allow-set-title", "off")
    live_gated = (
        isinstance(live_allow_set_title, str) and live_allow_set_title.strip().lower() == "off"
    )
    if file_gated or live_gated:
        where = []
        if file_gated:
            where.append(f"a directive in {path}")
        if live_gated:
            where.append("the running tmux server")
        warnings.append(
            "tmux 'allow-set-title off' is in effect ("
            + " and ".join(where)
            + "); Claude's activity glyph (OSC 2 pane title) is blocked. Remove "
            "it and restart tmux, or run `tmux set -gu allow-set-title`."
        )
    return warnings


# ── run-76 team-mode layout consistency — pane-add reconcile hook ───────────
#
# Phase-3 consensus (Option A, "hook-driven reconcile"): instead of Python
# GUESSING when Claude Code materialised a team-mode pane (the first-contact
# retitle heuristic), bind tmux's OWN pane-add event to the fold/reconcile
# entrypoint (``python3 -m scripts.fold_workspace``). The hook fires inside
# tmux's event pipeline, so it reacts to the REAL pane creation — closing the
# materialize-vs-fold race at the source.
#
# The three lettered Phase-3 concerns and how each is addressed here:
#
# CONCERN A — re-entrancy / infinite loop (BLOCKER).
#   The hook is bound to ``after-split-window`` — the command hook for the
#   ``split-window`` command, i.e. it fires on pane ADD only. Our own fold
#   runs ``select-layout`` and ``join-pane``, whose command hooks are
#   ``after-select-layout`` / ``after-join-pane`` — DIFFERENT hooks that we
#   never bind. So the fold cannot re-trigger the event it was started by.
#   (This is exactly why ``window-layout-changed`` — which DOES fire on
#   select-layout — was rejected in Phase 3.) Belt-and-suspenders per the
#   debate ("AND/OR guard with a re-entrancy lock"): the hook script ALSO
#   checks/sets a ``@kaizen_fold_hook_running`` window user-option around the
#   fold, so even a hypothetical future re-binding to a layout-change event
#   degrades to a single no-op re-fire instead of an unbounded loop. The
#   guard is advisory (run-shell -b is async, so two near-simultaneous
#   splits can both pass the check) — that TOCTOU is harmless because the
#   fold is reset-then-fold idempotent (kaizen#88); the guard's job is only
#   to break sequential hook→fold→hook recursion.
#
# CONCERN B — window-scope self-gate (BLOCKER).
#   tmux hooks are server-global (``set-hook -g``): the hook fires for EVERY
#   split in the operator's tmux server, including unrelated windows. The
#   hook script therefore self-gates on the ``@kaizen_team_id`` user-option
#   (the established labeled-identity pattern — kaizen#68): the install
#   helper tags the orchestrator's WINDOW with the team id, teammate PANES
#   are already tagged by ``apply_workspace_layout``, and tmux's user-option
#   format lookup falls back pane→window→session→global — so a freshly-split
#   pane in the kaizen window inherits the WINDOW tag before any pane-level
#   tag exists. The comparison itself happens in tmux's FORMAT layer
#   (``#{==:#{@kaizen_team_id},<id>}`` → literal 0/1), so the option VALUE —
#   which some other tool could have set to anything, quotes included —
#   never reaches /bin/sh; on any other window the conditional expands to 0
#   and the script exits without touching tmux at all.
#
# CONCERN C — env self-containment.
#   The hook command runs from the tmux SERVER's context, not the
#   orchestrator's shell: no inherited cwd, PYTHONPATH, or (reliably) $TMUX /
#   $TMUX_PANE. Every external reference in the command is therefore
#   absolute and embedded at install time: the python interpreter
#   (``sys.executable``), the kaizen root (cd + PYTHONPATH), the tmux binary
#   (for the guard set-option calls), the orchestrator's $TMUX value (so the
#   fold's own ``tmux`` subprocesses reach the RIGHT server/socket), and the
#   orchestrator's $TMUX_PANE. The TMUX_PANE embed is load-bearing twice
#   over: (1) tmux resolves a command client's "current window" from
#   TMUX_PANE, so the fold's un-targeted ``list-panes`` / ``select-layout``
#   land on the KAIZEN window even when the operator is viewing another
#   window when the hook fires; (2) ``_tmux_workspace._orchestrator_pane_id``
#   reads it to exclude the PM pane from the fold — embedding the
#   ORCHESTRATOR's pane id (not ``#{hook_pane}``, which would be the new
#   teammate pane) keeps that exclusion correct.
#
# Teardown (concern D, wired by AI-3): hooks live in the operator's
# long-running tmux server and survive the kaizen run — an un-removed hook
# would keep firing forever. The hook is installed under a NAMED ARRAY INDEX
# (``after-split-window[88]``) so ``set-hook -gu 'after-split-window[88]'``
# removes OUR entry and only ours, leaving any operator hooks at other
# indices untouched. Index 88 is a nod to the kaizen#88 reset-then-fold
# contract the hook relies on; any fixed high index works — it only needs to
# be stable and unlikely to collide (concurrent multi-repo kaizen runs are
# already barred, so a single well-known index is safe).

# The tmux event the reconcile hook binds to. MUST stay a pane-ADD command
# hook (see CONCERN A above) — never rebind to ``window-layout-changed`` /
# ``after-select-layout`` / ``after-join-pane`` without revisiting the
# re-entrancy analysis.
KAIZEN_TEAM_HOOK_EVENT = "after-split-window"
KAIZEN_TEAM_HOOK_INDEX = 88
KAIZEN_TEAM_HOOK_NAME = f"{KAIZEN_TEAM_HOOK_EVENT}[{KAIZEN_TEAM_HOOK_INDEX}]"

# Window user-option carrying the team id. Kept in literal sync with
# ``scripts._tmux_workspace.KAIZEN_TEAM_ID_OPTION`` (the pane-level tag) —
# duplicated rather than imported so this module stays decoupled from the
# subprocess-wrapper module (house rule: no cross-imports between subprocess
# wrappers); a test pins the two constants equal.
KAIZEN_TEAM_ID_OPTION = "@kaizen_team_id"

# Advisory re-entrancy lock (CONCERN A belt-and-suspenders) — window
# user-option set to "1" for the duration of a hook-triggered fold.
KAIZEN_FOLD_GUARD_OPTION = "@kaizen_fold_hook_running"

# Validation allowlists for everything interpolated into the hook command.
# The hook value crosses THREE parsers (tmux command parse at fire time →
# run-shell format expansion → /bin/sh), so we refuse anything outside a
# conservative charset instead of trying to escape for all three at once:
#   * no single/double quotes or backslash (would break sh and/or the tmux
#     double-quoted argument),
#   * no ``$`` (tmux substitutes environment variables inside double quotes),
#   * no ``#`` (run-shell expands ``#{...}`` formats; ``##`` escaping is not
#     worth the ambiguity),
#   * no ``;`` / whitespace / braces (tmux command separators / blocks).
_HOOK_TEAM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_HOOK_PANE_ID_RE = re.compile(r"^%\d+$")
_HOOK_ABS_PATH_RE = re.compile(r"^/[A-Za-z0-9._/+,:@=_-]*$")
# $TMUX is ``<socket-path>,<server-pid>,<session-idx>`` — the leading socket
# path makes it /-anchored and the path class above already admits commas and
# digits, so the SAME allowlist applies; aliased (not duplicated) so the two
# can never drift apart.
_HOOK_TMUX_ENV_RE = _HOOK_ABS_PATH_RE

# tmux invocation timeout — mirrors ``_tmux_workspace._TMUX_TIMEOUT_S``
# (local by design; see the no-cross-imports note above).
_TMUX_TIMEOUT_S = 10.0

_NO_SERVER_HINTS = (
    "no server running",
    "no current client",
    "can't find session",
    "no such session",
)


def _run_tmux(argv: list[str]) -> subprocess.CompletedProcess:
    """Run a tmux command with the house soft-failure defaults.

    check=False + captured text streams so callers can inspect
    returncode/stderr; a wedged server is bounded by ``_TMUX_TIMEOUT_S`` and
    surfaces as a synthetic returncode-124 result (coreutils ``timeout``
    convention) instead of a propagated TimeoutExpired.
    """
    try:
        return subprocess.run(
            ["tmux", *argv],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=_TMUX_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=["tmux", *argv],
            returncode=124,
            stdout="",
            stderr=f"tmux command timed out after {int(_TMUX_TIMEOUT_S)}s",
        )


def _tmux_unavailable(proc: subprocess.CompletedProcess) -> bool:
    """True iff ``proc`` failed with a "tmux server not running"-class error."""
    if proc.returncode == 0:
        return False
    blob = ((proc.stderr or "") + (proc.stdout or "")).lower()
    return any(hint in blob for hint in _NO_SERVER_HINTS)


def build_team_fold_hook_command(
    *,
    team_id: str,
    orchestrator_pane_id: str,
    kaizen_root: str,
    python_exe: str,
    tmux_exe: str,
    tmux_env: str,
) -> str:
    """Build the ``set-hook`` VALUE string for the team-window reconcile hook.

    Pure function (no I/O) so the exact command string is unit-testable. The
    returned value is one tmux command — ``run-shell -b "<sh script>"`` — that
    tmux parses at hook-fire time; ``run-shell`` then format-expands the
    script (``#{...}``) and hands it to ``/bin/sh``. ``-b`` keeps the fold off
    the tmux server's main loop (a blocking fold would freeze every client
    for its duration).

    Script shape (single line; single-quoted literals only — see the
    validation-charset comment above for why no escaping is attempted)::

        if [ '#{&&:#{==:#{@kaizen_team_id},<id>},
              #{!=:#{@kaizen_fold_hook_running},1}}' = '1' ]; then
            <guard on>; <fold>; <guard off>;
        fi

    Both option comparisons live in tmux's FORMAT layer (``#{==:...}`` /
    ``#{!=:...}``), not in sh: the combined conditional expands to a literal
    ``0`` or ``1`` before /bin/sh ever runs, so an option VALUE set by some
    other tool (e.g. an ``@kaizen_team_id`` containing a single quote) can
    never escape into the shell — the untrusted value is compared inside
    tmux and only the verdict reaches sh.

    Ordering is load-bearing: the team-id gate (CONCERN B) is evaluated
    before anything else, so on a foreign window the script performs zero
    side effects — no tmux calls, no python spawn. The guard set/unset is
    addressed via the embedded ORCHESTRATOR pane id (``-w`` resolves a pane
    target to its window): the orchestrator pane lives in the same window
    and outlives the run, whereas a ``#{hook_pane}`` target dies with the
    freshly-split pane — if that pane exited between guard-on and guard-off,
    the unset would fail and the stuck ``=1`` guard would mute the hook for
    the rest of the run. The fold's stdout/stderr go to /dev/null: the hook
    has no useful sink for them and ``fold_current_window`` already
    self-reports via the orchestrator path.

    Raises ValueError on any argument outside the conservative allowlists —
    the installer validates first and converts that to a warn-and-refuse.
    """
    checks = (
        (_HOOK_TEAM_ID_RE, team_id, "team_id"),
        (_HOOK_PANE_ID_RE, orchestrator_pane_id, "orchestrator_pane_id"),
        (_HOOK_ABS_PATH_RE, kaizen_root, "kaizen_root"),
        (_HOOK_ABS_PATH_RE, python_exe, "python_exe"),
        (_HOOK_ABS_PATH_RE, tmux_exe, "tmux_exe"),
        (_HOOK_TMUX_ENV_RE, tmux_env, "tmux_env"),
    )
    for pattern, value, name in checks:
        if not value or not pattern.fullmatch(value):
            raise ValueError(
                f"refusing to build tmux hook command: {name}={value!r} fails the "
                f"hook-safety allowlist {pattern.pattern!r}"
            )

    # Both comparisons happen in tmux's format layer: ``#{==:#{@opt},val}`` /
    # ``#{!=:#{@opt},val}`` expand to 0/1 and ``#{&&:a,b}`` combines them, so
    # the only thing run-shell hands to /bin/sh is a literal '0' or '1' —
    # never the (untrusted) option values themselves. tmux user-option
    # formats are written WITH the '@' (``#{@kaizen_team_id}``). The team_id
    # spliced into the ``==`` comparison is allowlist-validated above (no
    # ``,`` / ``}`` that could derail the format parse).
    gate_format = (
        f"#{{&&:#{{==:#{{{KAIZEN_TEAM_ID_OPTION}}},{team_id}}},"
        f"#{{!=:#{{{KAIZEN_FOLD_GUARD_OPTION}}},1}}}}"
    )
    gate = f"[ '{gate_format}' = '1' ]"
    # Guard toggles target the ORCHESTRATOR pane (stable for the whole run;
    # ``-w`` resolves it to the shared window) — NOT ``#{hook_pane}``, whose
    # pane can die between guard-on and guard-off and strand the guard at 1,
    # muting every later hook fire.
    guard_on = (
        f"env TMUX='{tmux_env}' '{tmux_exe}' set-option -w -t '{orchestrator_pane_id}' "
        f"{KAIZEN_FOLD_GUARD_OPTION} 1"
    )
    fold = (
        f"cd '{kaizen_root}' && env TMUX='{tmux_env}' TMUX_PANE='{orchestrator_pane_id}' "
        f"PYTHONPATH='{kaizen_root}' '{python_exe}' -m scripts.fold_workspace "
        f"--team-id '{team_id}' >/dev/null 2>&1"
    )
    guard_off = (
        f"env TMUX='{tmux_env}' '{tmux_exe}' set-option -wu -t '{orchestrator_pane_id}' "
        f"{KAIZEN_FOLD_GUARD_OPTION}"
    )
    script = f"if {gate}; then {guard_on}; {fold}; {guard_off}; fi"
    # Defensive invariant: the script must survive tmux's double-quote parse
    # verbatim. The allowlists make this unreachable; assert anyway so a
    # future edit cannot silently ship a truncated hook.
    if any(ch in script for ch in ('"', "\\", "$")):
        raise ValueError("hook script contains characters unsafe for tmux double-quoting")
    return f'run-shell -b "{script}"'


def install_team_window_hook(
    team_id: str,
    *,
    kaizen_root: str | Path | None = None,
    python_exe: str | None = None,
    tmux_exe: str | None = None,
) -> bool:
    """Install the pane-add reconcile hook for the kaizen team window.

    Two tmux writes, in a deliberate order:

      1. Tag the orchestrator's WINDOW with ``@kaizen_team_id=<team_id>``
         (CONCERN B): freshly-split panes inherit the window option in format
         lookups, so the hook's self-gate matches in the kaizen window before
         any pane-level tag exists, and matches nowhere else.
      2. Bind the global ``after-split-window[88]`` hook to the built
         reconcile command.

    Tag-before-hook means the hook can never fire ungated; if the tag write
    fails, the hook is NOT installed (an always-false gate would merely be
    inert, but refusing keeps install atomic-ish and the failure visible).

    Must be called from the orchestrator session (needs $TMUX / $TMUX_PANE to
    self-locate). Best-effort contract, parity with the rest of the tmux
    helpers: returns True on success, False (with a stderr warning where
    actionable) when tmux is unavailable, the env is missing, or any value
    fails the hook-safety allowlist. Never raises.
    """
    pane_id = os.environ.get("TMUX_PANE", "").strip()
    tmux_env = os.environ.get("TMUX", "").strip()
    if not pane_id or not tmux_env:
        print(
            "[_tmux_config] install_team_window_hook skipped: TMUX/TMUX_PANE not set "
            "(orchestrator is not inside a tmux pane); pane-add reconcile hook not installed.",
            file=sys.stderr,
        )
        return False
    resolved_root = str(
        Path(kaizen_root).resolve() if kaizen_root else Path(__file__).resolve().parent.parent
    )
    resolved_python = python_exe or sys.executable
    resolved_tmux = tmux_exe or shutil.which("tmux") or ""
    try:
        hook_command = build_team_fold_hook_command(
            team_id=team_id,
            orchestrator_pane_id=pane_id,
            kaizen_root=resolved_root,
            python_exe=resolved_python,
            tmux_exe=resolved_tmux,
            tmux_env=tmux_env,
        )
    except ValueError as exc:
        print(
            f"[_tmux_config] install_team_window_hook skipped: {exc}",
            file=sys.stderr,
        )
        return False

    tag_proc = _run_tmux(["set-option", "-w", "-t", pane_id, KAIZEN_TEAM_ID_OPTION, team_id])
    if tag_proc.returncode != 0:
        if not _tmux_unavailable(tag_proc):
            print(
                f"[_tmux_config] install_team_window_hook: window tag "
                f"{KAIZEN_TEAM_ID_OPTION}={team_id} failed "
                f"({(tag_proc.stderr or '').strip()}); hook not installed.",
                file=sys.stderr,
            )
        return False
    hook_proc = _run_tmux(["set-hook", "-g", KAIZEN_TEAM_HOOK_NAME, hook_command])
    if hook_proc.returncode != 0:
        if not _tmux_unavailable(hook_proc):
            print(
                f"[_tmux_config] install_team_window_hook: set-hook -g "
                f"{KAIZEN_TEAM_HOOK_NAME} failed: {(hook_proc.stderr or '').strip()}",
                file=sys.stderr,
            )
        return False
    return True


def remove_team_window_hook(*, orchestrator_pane_id: str | None = None) -> bool:
    """Tear down the pane-add reconcile hook (and ONLY ours).

    ``set-hook -gu 'after-split-window[88]'`` unsets the single array entry
    kaizen installed; operator hooks on the same event at other indices are
    untouched. Also clears the window-scoped ``@kaizen_team_id`` tag and any
    stale ``@kaizen_fold_hook_running`` guard flag (a fold killed mid-flight
    would otherwise leave the guard set, muting every future hook fire on
    that window).

    The hook unset is the success criterion — an un-removed hook keeps
    firing in the operator's long-lived tmux server forever (concern D). The
    option unsets are best-effort extras: when ``orchestrator_pane_id`` is
    None we fall back to $TMUX_PANE, and when neither is available we skip
    them (the tag is inert without the hook). Never raises.
    """
    unhook_proc = _run_tmux(["set-hook", "-gu", KAIZEN_TEAM_HOOK_NAME])
    unhook_ok = unhook_proc.returncode == 0
    if not unhook_ok and not _tmux_unavailable(unhook_proc):
        print(
            f"[_tmux_config] remove_team_window_hook: set-hook -gu "
            f"{KAIZEN_TEAM_HOOK_NAME} failed: {(unhook_proc.stderr or '').strip()}",
            file=sys.stderr,
        )
    pane_id = orchestrator_pane_id or os.environ.get("TMUX_PANE", "").strip()
    if pane_id:
        for option in (KAIZEN_TEAM_ID_OPTION, KAIZEN_FOLD_GUARD_OPTION):
            opt_proc = _run_tmux(["set-option", "-wu", "-t", pane_id, option])
            if opt_proc.returncode != 0 and not _tmux_unavailable(opt_proc):
                print(
                    f"[_tmux_config] remove_team_window_hook: unset {option} on "
                    f"{pane_id} failed: {(opt_proc.stderr or '').strip()}",
                    file=sys.stderr,
                )
    return unhook_ok
