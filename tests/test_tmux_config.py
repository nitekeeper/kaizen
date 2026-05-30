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


# ── CONFIG_BLOCK content (kaizen#76 — dual-signal pane-border-format) ─────


def test_marker_version_bumped_to_4():
    """kaizen#79 — MARKER_VERSION must bump to 4 for the agent-indicator block.

    The bump is the forcing-function that drives setup.py's "update
    available" branch on existing installs; without it, operators with a
    v3 marker would not be prompted to refresh their tmux.conf to pick up
    the optional tmux-agent-indicator detect-and-source integration.
    """
    assert MARKER_VERSION == 4


def test_config_block_renders_activity_glyph_prefix():
    """kaizen#76 — CONFIG_BLOCK must render the OSC 2 activity glyph.

    ``#{=1:pane_title}`` is the tmux FORMATS construct "take the first 1
    character of ``pane_title``"; this preserves Claude Code's idle dot /
    busy spinner that the subagent process emits via OSC 2 even after we
    override the kaizen-owned label via ``@desired_title``.
    """
    assert "#{=1:pane_title}" in CONFIG_BLOCK


def test_config_block_renders_desired_title_token():
    """kaizen#76 — CONFIG_BLOCK must render @desired_title with pane_title fallback.

    The kaizen-owned wave/role label lives in the ``@desired_title``
    per-pane user-option (immune to OSC 2 overrides); the fallback to
    ``pane_title`` keeps pre-spawn panes (e.g. a bare ``zsh``) readable.
    Asserted together with the glyph prefix to enforce the S6 dual-signal
    Iron Law: both signals must be present in the rendered border.
    """
    assert "#{?@desired_title,#{@desired_title},#{pane_title}}" in CONFIG_BLOCK


# ── CONFIG_BLOCK content (kaizen#79 — tmux-agent-indicator detect-and-source) ─


def test_config_block_has_plugin_detection_guard():
    """kaizen#79 — integration is gated behind an if-shell -b presence check.

    The guard re-evaluates plugin presence at config LOAD time (robust if the
    operator installs the plugin AFTER kaizen wrote the block) and is a
    harmless no-op when the dir is absent.
    """
    assert "if-shell -b" in CONFIG_BLOCK
    assert '[ -d "$HOME/.tmux/plugins/tmux-agent-indicator" ]' in CONFIG_BLOCK


def test_config_block_sources_plugin_bootstrap():
    """kaizen#79 — the present-branch sources the plugin bootstrap with -q.

    ``-q`` ensures a missing/renamed bootstrap never errors the config load.
    """
    assert (
        "source-file -q '$HOME/.tmux/plugins/tmux-agent-indicator/agent-indicator.tmux'"
        in CONFIG_BLOCK
    )


def test_config_block_adds_agent_indicator_status_right():
    """kaizen#79 — the #{agent_indicator} placeholder must reach status-right.

    Without a status-right placeholder the plugin renders nothing — this is
    the one line that actually surfaces the indicator.
    """
    assert "#{agent_indicator}" in CONFIG_BLOCK
    assert "set -g status-right '#{agent_indicator} | %H:%M'" in CONFIG_BLOCK


def test_config_block_sets_claude_icon_option():
    """kaizen#79 — the Claude icon is the claude= entry in @agent-indicator-icons.

    There is no standalone @agent-indicator-icon-claude option; the icon map
    is the single @agent-indicator-icons option with claude=🤖 as the default.
    """
    assert (
        "set -g @agent-indicator-icons 'claude=🤖,codex=🧠,opencode=💻,default=🤖'" in CONFIG_BLOCK
    )


def test_config_block_composite_render_preserved_as_fallback():
    """kaizen#79 — the kaizen#76 composite render stays set UNCONDITIONALLY.

    It must appear OUTSIDE the if-shell guard so it is the zero-dependency
    fallback when the plugin is absent and still carries the wave/role label
    when present. We assert it is not nested inside the if-shell command
    string by checking it precedes the guard in the block.
    """
    border_line = (
        "set -g pane-border-format "
        "'#{=1:pane_title} #[fg=cyan]#{?@desired_title,#{@desired_title},#{pane_title}}#[default]'"
    )
    assert border_line in CONFIG_BLOCK
    # The unconditional border render must come BEFORE the if-shell guard.
    assert CONFIG_BLOCK.index(border_line) < CONFIG_BLOCK.index("if-shell -b")


def test_config_block_never_auto_installs_or_mutates_global_config():
    """kaizen#79 — kaizen NEVER installs the plugin or writes global config.

    The integration is purely additive tmux directives; the block must contain
    no installer invocation and no mutation of the operator's settings/conf.
    """
    forbidden = (
        "curl",
        "install.sh",
        ".claude/settings.json",
        "allow-passthrough",
    )
    for token in forbidden:
        assert token not in CONFIG_BLOCK, f"CONFIG_BLOCK must not contain {token!r}"
    # The block must not write the user's tmux.conf either (it IS the content
    # that gets written, via consent flow — it must not self-mutate ~/.tmux.conf).
    assert "~/.tmux.conf" not in CONFIG_BLOCK
    assert ".tmux.conf" not in CONFIG_BLOCK


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
