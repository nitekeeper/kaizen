"""Tests for scripts/_tmux_config.py — agent-teams tmux block helpers."""

from __future__ import annotations

import pytest

from scripts._tmux_config import (
    CONFIG_BLOCK,
    MARKER_END,
    MARKER_START,
    MARKER_VERSION,
    apply_config_block,
    detect_existing_marker,
    show_diff,
)

# ── detect_existing_marker ────────────────────────────────────────────────


def test_detect_existing_marker_returns_none_when_file_missing(tmp_path):
    """File that does not exist on disk → None (not an error)."""
    assert detect_existing_marker(tmp_path / "no-such-file") is None


def test_detect_existing_marker_returns_none_when_no_marker_present(tmp_path):
    """File without any marker → None even when it has tmux content."""
    p = tmp_path / "tmux.conf"
    p.write_text("set -g status on\n")
    assert detect_existing_marker(p) is None


def test_detect_existing_marker_returns_version_when_v1_marker_present(tmp_path):
    """File with a well-formed v1 marker block → 1."""
    p = tmp_path / "tmux.conf"
    p.write_text(
        f"set -g status on\n\n{MARKER_START.format(1)}\n{CONFIG_BLOCK}{MARKER_END.format(1)}\n"
    )
    assert detect_existing_marker(p) == 1


def test_detect_existing_marker_raises_when_malformed(tmp_path):
    """A marker whose version portion can't parse must raise ValueError."""
    p = tmp_path / "tmux.conf"
    p.write_text("# >>> agent-teams vXYZ >>>\nstuff\n# <<< agent-teams vXYZ <<<\n")
    with pytest.raises(ValueError) as exc:
        detect_existing_marker(p)
    assert "Malformed" in str(exc.value)


def test_detect_existing_marker_raises_when_start_end_versions_disagree(tmp_path):
    """START v1 + END v2 is illegal — must surface as ValueError."""
    p = tmp_path / "tmux.conf"
    p.write_text("# >>> agent-teams v1 >>>\nbody\n# <<< agent-teams v2 <<<\n")
    with pytest.raises(ValueError) as exc:
        detect_existing_marker(p)
    assert "disagree" in str(exc.value) or "Malformed" in str(exc.value)


# ── apply_config_block ────────────────────────────────────────────────────


def test_apply_config_block_creates_file_when_missing(tmp_path):
    p = tmp_path / "tmux.conf"
    apply_config_block(p, MARKER_VERSION)
    assert p.exists()
    text = p.read_text()
    assert MARKER_START.format(MARKER_VERSION) in text
    assert MARKER_END.format(MARKER_VERSION) in text
    # The CONFIG_BLOCK body must be present verbatim.
    assert "pane-border-status top" in text


def test_apply_config_block_appends_with_blank_line_when_no_marker(tmp_path):
    """Pre-existing content + no marker → append separated by one blank line."""
    p = tmp_path / "tmux.conf"
    p.write_text("set -g status on\n")
    apply_config_block(p, MARKER_VERSION)
    text = p.read_text()
    # Original content is preserved.
    assert text.startswith("set -g status on\n")
    # The block appears AFTER the original content.
    assert "set -g status on" in text.split(MARKER_START.format(MARKER_VERSION))[0]
    # One blank-line separator: the marker is preceded by an empty line.
    pre_marker = text.split(MARKER_START.format(MARKER_VERSION))[0]
    assert pre_marker.endswith("\n\n"), (
        f"expected one blank-line separator before marker; got: {pre_marker!r}"
    )


def test_apply_config_block_replaces_existing_block_in_place(tmp_path):
    """Marker present already → replace the block, leaving other content intact."""
    p = tmp_path / "tmux.conf"
    p.write_text(
        "set -g status on\n\n"
        f"{MARKER_START.format(1)}\n"
        "OLD BLOCK CONTENT\n"
        f"{MARKER_END.format(1)}\n"
        "set -g mouse on\n"
    )
    apply_config_block(p, 2)
    text = p.read_text()
    # Old content (above and below the block) is preserved.
    assert "set -g status on" in text
    assert "set -g mouse on" in text
    # Old block content is gone.
    assert "OLD BLOCK CONTENT" not in text
    # New marker is present at v2.
    assert MARKER_START.format(2) in text
    assert MARKER_END.format(2) in text
    # Exactly one marker block in the file.
    assert text.count(MARKER_START.format(2)) == 1


def test_apply_config_block_is_idempotent_at_same_version(tmp_path):
    """Calling twice in a row with no edits between yields byte-identical files."""
    p = tmp_path / "tmux.conf"
    p.write_text("set -g status on\n")
    apply_config_block(p, MARKER_VERSION)
    first = p.read_text()
    apply_config_block(p, MARKER_VERSION)
    second = p.read_text()
    assert first == second


# ── show_diff ─────────────────────────────────────────────────────────────


def test_show_diff_returns_create_message_when_file_missing(tmp_path):
    p = tmp_path / "no-such-file.conf"
    diff = show_diff(p, MARKER_VERSION)
    assert "create" in diff.lower()
    assert "pane-border-status" in diff


def test_show_diff_returns_append_message_when_no_marker(tmp_path):
    p = tmp_path / "tmux.conf"
    p.write_text("set -g status on\n")
    diff = show_diff(p, MARKER_VERSION)
    assert "append" in diff.lower()


def test_show_diff_returns_noop_when_version_matches(tmp_path):
    p = tmp_path / "tmux.conf"
    apply_config_block(p, MARKER_VERSION)
    diff = show_diff(p, MARKER_VERSION)
    assert "no-op" in diff.lower()
    assert f"v{MARKER_VERSION}" in diff


def test_show_diff_returns_update_message_for_older_marker(tmp_path):
    p = tmp_path / "tmux.conf"
    p.write_text(f"{MARKER_START.format(1)}\nOLD\n{MARKER_END.format(1)}\n")
    diff = show_diff(p, 2)
    assert "update" in diff.lower()
    assert "v1" in diff
    assert "v2" in diff


# ── setup.py consent flow ─────────────────────────────────────────────────


def test_check_tmux_config_creates_file_when_missing(tmp_path, monkeypatch, capsys):
    """Branch 1 of _check_tmux_config: file missing + user says Y → file is created."""
    from scripts import setup as setup_mod

    target = tmp_path / "no_tmux_yet.conf"
    monkeypatch.setattr(setup_mod, "_locate_tmux_conf", lambda: target)
    # User accepts (default empty = yes).
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    setup_mod._check_tmux_config()
    assert target.exists()
    text = target.read_text()
    assert MARKER_START.format(MARKER_VERSION) in text


def test_check_tmux_config_skips_when_user_declines(tmp_path, monkeypatch):
    """File missing but user says 'n' → file is NOT created."""
    from scripts import setup as setup_mod

    target = tmp_path / "still_missing.conf"
    monkeypatch.setattr(setup_mod, "_locate_tmux_conf", lambda: target)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    setup_mod._check_tmux_config()
    assert not target.exists()


def test_check_tmux_config_appends_when_file_exists_no_marker(tmp_path, monkeypatch):
    """Branch 2: file exists + no marker + user says Y → block is appended."""
    from scripts import setup as setup_mod

    target = tmp_path / "tmux.conf"
    target.write_text("set -g status on\n")
    monkeypatch.setattr(setup_mod, "_locate_tmux_conf", lambda: target)
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    setup_mod._check_tmux_config()
    text = target.read_text()
    assert "set -g status on" in text
    assert MARKER_START.format(MARKER_VERSION) in text


def test_check_tmux_config_noop_when_current_version(tmp_path, monkeypatch, capsys):
    """Branch 3: file exists at current version → silent info line, no edit."""
    from scripts import setup as setup_mod

    target = tmp_path / "tmux.conf"
    apply_config_block(target, MARKER_VERSION)
    before = target.read_text()
    monkeypatch.setattr(setup_mod, "_locate_tmux_conf", lambda: target)
    # input() should NOT be called in the up-to-date branch — fail loudly if it is.

    def _no_input(_prompt):
        raise AssertionError("input() should not be called for the up-to-date branch")

    monkeypatch.setattr("builtins.input", _no_input)
    setup_mod._check_tmux_config()
    assert target.read_text() == before
    out = capsys.readouterr().out
    assert "up-to-date" in out


def test_check_tmux_config_updates_when_older_marker(tmp_path, monkeypatch):
    """Branch 4: file has an older marker + user says Y → block is replaced."""
    from scripts import setup as setup_mod

    target = tmp_path / "tmux.conf"
    target.write_text(f"{MARKER_START.format(0)}\nold\n{MARKER_END.format(0)}\n")
    monkeypatch.setattr(setup_mod, "_locate_tmux_conf", lambda: target)
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    setup_mod._check_tmux_config()
    text = target.read_text()
    # Old version marker should be gone; new version marker should be present.
    assert MARKER_START.format(0) not in text
    assert MARKER_START.format(MARKER_VERSION) in text
