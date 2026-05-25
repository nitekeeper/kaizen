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

MARKER_VERSION = 1
MARKER_START = "# >>> agent-teams v{} >>>"
MARKER_END = "# <<< agent-teams v{} <<<"

# Verbatim from the spec — keep this in sync with any future MARKER_VERSION bumps.
CONFIG_BLOCK = """# Pane border format — show pane_title (set by app post-spawn)
set -g pane-border-status top
set -g pane-border-format '#[fg=cyan]#{pane_title}#[default]'

# Default layout for new windows: main-vertical
# Apps may override per-window via select-layout.
set -g main-pane-width 60
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
