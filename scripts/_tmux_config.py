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


def apply_config_block(tmux_conf_path: Path, version: int) -> None:
    """Install or replace the agent-teams block in ``tmux_conf_path``.

    If the file does not exist, create it containing only the block.
    If the file exists but has no marker, append the block (with one
    blank line separating it from existing content).
    If the file already has a marker, REPLACE it in place with the
    current version block.

    Idempotent: calling twice at the same version with no edits between
    leaves the file byte-identical the second time.
    """
    existing = _read_text(tmux_conf_path)
    block = _full_block(version)
    if not existing:
        tmux_conf_path.parent.mkdir(parents=True, exist_ok=True)
        tmux_conf_path.write_text(block, encoding="utf-8")
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
        tmux_conf_path.write_text(stripped + sep + block, encoding="utf-8")
        return
    # No marker — append with one blank line separator.
    sep = ""
    if not existing.endswith("\n"):
        sep = "\n\n"
    elif not existing.endswith("\n\n"):
        sep = "\n"
    tmux_conf_path.write_text(existing + sep + block, encoding="utf-8")


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
