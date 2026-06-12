"""Tests for scripts/_tmux_config.py — agent-teams tmux block helpers."""

from __future__ import annotations

import os
import subprocess as real_subprocess

import pytest

from scripts import _tmux_config
from scripts._tmux_config import (
    CONFIG_BLOCK,
    KAIZEN_FOLD_GUARD_OPTION,
    KAIZEN_TEAM_HOOK_EVENT,
    KAIZEN_TEAM_HOOK_NAME,
    KAIZEN_TEAM_ID_OPTION,
    MARKER_END,
    MARKER_START,
    MARKER_VERSION,
    apply_config_block,
    build_team_fold_hook_command,
    check_glyph_readiness,
    detect_existing_marker,
    extract_installed_block,
    install_team_window_hook,
    remove_team_window_hook,
    removed_glyph_gating_directives,
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


# ── apply_config_block — safe-write semantics (backup / symlink / atomic) ──


def test_apply_creates_backup_of_prior_content(tmp_path):
    """Iron-Law (pre-fix failure): replacing an existing conf must first copy
    the PRIOR state to <name>.kaizen.bak — the .bak holds the original bytes."""
    p = tmp_path / "tmux.conf"
    original = f"set -g status on\n\n{MARKER_START.format(1)}\nOLD BLOCK\n{MARKER_END.format(1)}\n"
    p.write_text(original)
    apply_config_block(p, MARKER_VERSION)
    bak = tmp_path / "tmux.conf.kaizen.bak"
    assert bak.exists(), "expected a .kaizen.bak backup of the prior conf"
    assert bak.read_text() == original, "backup must equal the ORIGINAL bytes"


def test_apply_creates_backup_when_appending_to_unmarked_file(tmp_path):
    """The append branch (existing content, no marker) also backs up first."""
    p = tmp_path / "tmux.conf"
    p.write_text("set -g status on\n")
    apply_config_block(p, MARKER_VERSION)
    bak = tmp_path / "tmux.conf.kaizen.bak"
    assert bak.exists()
    assert bak.read_text() == "set -g status on\n"


def test_apply_no_backup_when_creating_fresh_file(tmp_path):
    """The create branch has no prior state — no .bak is produced."""
    p = tmp_path / "tmux.conf"
    apply_config_block(p, MARKER_VERSION)
    assert not (tmp_path / "tmux.conf.kaizen.bak").exists()


def test_apply_preserves_symlinked_conf(tmp_path):
    """A symlinked ~/.tmux.conf must STAY a symlink with the block landing in
    the real file (and the .bak beside the real file).

    DELIBERATE anti-regression: the symlink-preservation half of this test is
    green pre-fix (write_text follows symlinks) — it exists to pin the
    behaviour against a naive ``os.replace(tmp, path)`` implementation, which
    would silently replace the symlink itself with a regular file.
    """
    real = tmp_path / "real" / "conf"
    real.parent.mkdir()
    real.write_text("set -g status on\n")
    link = tmp_path / "tmux.conf"
    link.symlink_to(real)

    apply_config_block(link, MARKER_VERSION)

    assert link.is_symlink(), "the symlink must survive apply_config_block"
    text = real.read_text()
    assert MARKER_START.format(MARKER_VERSION) in text, "block must land in the REAL file"
    assert "set -g status on" in text
    bak = real.parent / "conf.kaizen.bak"
    assert bak.exists(), "backup must land beside the REAL file, not the symlink"
    assert bak.read_text() == "set -g status on\n"


def test_apply_leaves_no_tmp_droppings(tmp_path):
    """The atomic-write temp file must never survive a successful apply."""
    p = tmp_path / "tmux.conf"
    p.write_text("set -g status on\n")
    apply_config_block(p, MARKER_VERSION)
    apply_config_block(p, MARKER_VERSION)  # replace branch too
    droppings = list(tmp_path.rglob("*.kaizen.tmp*"))
    assert droppings == [], f"temp-file droppings left behind: {droppings}"


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


# ── kaizen#98 Gap A — removed_glyph_gating_directives / extract block ──────

# A representative OLD v2 block body: glyph-less border + the confirmed
# glyph-gating ``allow-set-title off`` landmine.
_V2_BLOCK_BODY = (
    "set -g pane-border-status top\n"
    "set -g pane-border-format '#[fg=cyan]#{pane_title}#[default]'\n"
    "set -g allow-set-title off\n"
)


def test_extract_installed_block_returns_body_between_markers(tmp_path):
    p = tmp_path / "tmux.conf"
    p.write_text(
        f"set -g status on\n\n{MARKER_START.format(2)}\n{_V2_BLOCK_BODY}{MARKER_END.format(2)}\n"
    )
    body = extract_installed_block(p)
    assert "allow-set-title off" in body
    # Marker lines themselves are excluded.
    assert "agent-teams" not in body
    # Content outside the block is excluded.
    assert "status on" not in body


def test_extract_installed_block_empty_when_no_marker(tmp_path):
    p = tmp_path / "tmux.conf"
    p.write_text("set -g status on\n")
    assert extract_installed_block(p) == ""


def test_extract_installed_block_empty_when_file_missing(tmp_path):
    assert extract_installed_block(tmp_path / "nope.conf") == ""


def test_removed_glyph_gating_detects_allow_set_title_drop():
    """v2 (allow-set-title off) → v4 (no such directive) ⇒ directive reported."""
    removed = removed_glyph_gating_directives(_V2_BLOCK_BODY, CONFIG_BLOCK)
    assert removed == ["allow-set-title off"]


def test_removed_glyph_gating_empty_when_new_block_also_sets_it():
    """If BOTH old and new set the gate, it was not removed → not reported."""
    new_with_gate = CONFIG_BLOCK + "\nset -g allow-set-title off\n"
    assert removed_glyph_gating_directives(_V2_BLOCK_BODY, new_with_gate) == []


def test_removed_glyph_gating_empty_when_old_never_set_it():
    """A v3-style block with no gate → nothing to remove."""
    old_no_gate = "set -g pane-border-status top\nset -g main-pane-width 60\n"
    assert removed_glyph_gating_directives(old_no_gate, CONFIG_BLOCK) == []


def test_removed_glyph_gating_matches_set_command_variants():
    """`set-option` / `setw` / extra flags all parse as a gate."""
    for variant in (
        "set-option -g allow-set-title off",
        "setw -gq allow-set-title off",
        "set allow-set-title off",
        "set -g allow-set-title Off",  # case-insensitive value
    ):
        assert removed_glyph_gating_directives(variant + "\n", CONFIG_BLOCK) == [
            "allow-set-title off"
        ], variant


def test_removed_glyph_gating_matches_quoted_value():
    """A quoted gate value (`'off'` / `"off"`) is detected (kaizen#98 NIT)."""
    for variant in ("set -g allow-set-title 'off'", 'set -g allow-set-title "off"'):
        assert removed_glyph_gating_directives(variant + "\n", CONFIG_BLOCK) == [
            "allow-set-title off"
        ], variant


def test_removed_glyph_gating_ignores_unset_line():
    """An UNSET (`set -gu allow-set-title`, no value) is not a gate (kaizen#98 NIT).

    The flag cluster `-gu` must not be misparsed as the option name and the
    valueless line must not register as setting the gate to off.
    """
    assert removed_glyph_gating_directives("set -gu allow-set-title\n", CONFIG_BLOCK) == []


def test_canonical_config_block_does_not_gate_the_glyph():
    """Guardrail: the shipped v4 block must NOT set allow-set-title off."""
    assert removed_glyph_gating_directives("", CONFIG_BLOCK) == []
    assert "allow-set-title off" not in CONFIG_BLOCK


# ── kaizen#98 Gap B — check_glyph_readiness ────────────────────────────────


def test_check_glyph_readiness_fresh_v4_returns_empty(tmp_path):
    """A current v4 install with no gate → no warnings."""
    p = tmp_path / "tmux.conf"
    apply_config_block(p, MARKER_VERSION)
    assert check_glyph_readiness(p) == []


def test_check_glyph_readiness_warns_on_stale_marker(tmp_path):
    p = tmp_path / "tmux.conf"
    p.write_text(f"{MARKER_START.format(2)}\n{_V2_BLOCK_BODY}{MARKER_END.format(2)}\n")
    warnings = check_glyph_readiness(p)
    # Stale-version warning AND the allow-set-title-off file warning fire.
    assert any("v2" in w and f"v{MARKER_VERSION}" in w for w in warnings)
    assert any("allow-set-title off" in w for w in warnings)


def test_check_glyph_readiness_warns_on_live_allow_set_title_off(tmp_path):
    """Fresh v4 file but the RUNNING server reports off → live warning."""
    p = tmp_path / "tmux.conf"
    apply_config_block(p, MARKER_VERSION)
    warnings = check_glyph_readiness(p, live_allow_set_title="off")
    assert len(warnings) == 1
    assert "running tmux server" in warnings[0]
    assert "allow-set-title off" in warnings[0]


def test_check_glyph_readiness_tolerates_missing_file(tmp_path):
    assert check_glyph_readiness(tmp_path / "nope.conf") == []


def test_check_glyph_readiness_malformed_marker_is_warning_not_raise(tmp_path):
    p = tmp_path / "tmux.conf"
    p.write_text("# >>> agent-teams vXYZ >>>\nbody\n# <<< agent-teams vXYZ <<<\n")
    warnings = check_glyph_readiness(p)
    assert len(warnings) == 1
    assert "malformed" in warnings[0].lower()


# ── run-76 AI-2 — pane-add reconcile hook (install / teardown / command) ───
#
# Test strategy mirrors the established tmux-boundary pattern from
# tests/test_tmux_workspace.py: monkeypatch ``<module>.subprocess.run`` with a
# fake that records argv and returns canned CompletedProcess results — no
# live tmux server anywhere. On top of that, the hook SCRIPT itself (the
# ``run-shell -b "<sh>"`` payload) is exercised functionally by simulating
# tmux's format expansion (plain string substitution of the ``#{...}``
# tokens, exactly what run-shell does before handing the script to /bin/sh)
# and running the result with /bin/sh against recorder stubs standing in for
# the python interpreter and the tmux binary. That gives REAL no-leak /
# no-loop evidence (the Phase-3 "tests FIRST" pair) without inventing a tmux
# mock contract.

_HOOK_KW = {
    "team_id": "tid-123",
    "orchestrator_pane_id": "%1",
    "kaizen_root": "/home/op/apps/kaizen",
    "python_exe": "/usr/bin/python3",
    "tmux_exe": "/usr/bin/tmux",
    "tmux_env": "/tmp/tmux-1000/default,12345,3",
}


def _mk_proc(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return real_subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _extract_hook_script(hook_command: str) -> str:
    """Peel the sh script out of the ``run-shell -b "<script>"`` tmux command."""
    prefix = 'run-shell -b "'
    assert hook_command.startswith(prefix), hook_command
    assert hook_command.endswith('"'), hook_command
    return hook_command[len(prefix) : -1]


def _expand_formats(script: str, *, team_opt: str, guard_opt: str, team_id: str = "tid-123") -> str:
    """Simulate run-shell's #{...} format expansion at hook-fire time.

    The script's only format is the combined gate conditional
    ``#{&&:#{==:#{@kaizen_team_id},<id>},#{!=:#{@kaizen_fold_hook_running},1}}``
    — tmux evaluates the option comparisons in the FORMAT layer and expands
    the whole token to a literal ``0``/``1`` (the option VALUES never reach
    /bin/sh). ``team_opt`` / ``guard_opt`` are what the user-options hold in
    the fired-on window: the team id (kaizen window), '' (foreign / untagged
    window), or '1' (guard set mid-fold); the shim computes the verdict the
    way tmux would.
    """
    gate_token = (
        f"#{{&&:#{{==:#{{{KAIZEN_TEAM_ID_OPTION}}},{team_id}}},"
        f"#{{!=:#{{{KAIZEN_FOLD_GUARD_OPTION}}},1}}}}"
    )
    assert gate_token in script, f"gate format token not found in script: {script}"
    verdict = "1" if (team_opt == team_id and guard_opt != "1") else "0"
    expanded = script.replace(gate_token, verdict)
    # Drift guard: after expanding the gate the script must be fully concrete
    # — a leftover #{...} would mean the shim no longer mirrors run-shell.
    assert "#{" not in expanded, f"unexpanded format remains: {expanded}"
    return expanded


def _write_recorders(tmp_path):
    """Create executable sh stubs for python/tmux that append to one log.

    A shared log file preserves the relative ORDER of guard-set / fold /
    guard-unset calls. The fake python dumps argv + the env vars the fold
    entrypoint depends on (concern C evidence); the fake tmux dumps argv.
    """
    log = tmp_path / "calls.log"
    fake_python = tmp_path / "fake_python"
    fake_python.write_text(
        "#!/bin/sh\n"
        f'printf \'PY|%s|TMUX=%s|TMUX_PANE=%s|PYTHONPATH=%s|CWD=%s\\n\' "$*" "$TMUX" '
        f'"$TMUX_PANE" "$PYTHONPATH" "$(pwd)" >> {log}\n'
    )
    fake_python.chmod(0o755)
    fake_tmux = tmp_path / "fake_tmux"
    fake_tmux.write_text(f"#!/bin/sh\nprintf 'TMUX|%s\\n' \"$*\" >> {log}\n")
    fake_tmux.chmod(0o755)
    return log, str(fake_python), str(fake_tmux)


def _build_with_recorders(tmp_path):
    log, fake_python, fake_tmux = _write_recorders(tmp_path)
    cmd = build_team_fold_hook_command(
        team_id="tid-123",
        orchestrator_pane_id="%1",
        kaizen_root=str(tmp_path),  # must exist — the script cd's into it
        python_exe=fake_python,
        tmux_exe=fake_tmux,
        tmux_env="/tmp/tmux-1000/default,12345,3",
    )
    return log, _extract_hook_script(cmd)


def _run_sh(script: str) -> real_subprocess.CompletedProcess:
    return real_subprocess.run(
        ["/bin/sh", "-c", script], capture_output=True, text=True, timeout=30
    )


# ── constants / structure ─────────────────────────────────────────────────


def test_team_id_option_constant_matches_workspace_module():
    """The window-tag option name is duplicated (no cross-imports between the
    subprocess-wrapper modules) — pin the two literals equal so they can
    never drift apart silently."""
    from scripts._tmux_workspace import KAIZEN_TEAM_ID_OPTION as workspace_option

    assert workspace_option == KAIZEN_TEAM_ID_OPTION


def test_hook_binds_pane_add_event_only():
    """Concern A (primary mechanism): the hook MUST bind after-split-window —
    a pane-ADD command hook that our own fold (select-layout + join-pane)
    never emits — and must be array-indexed so teardown removes only ours."""
    assert KAIZEN_TEAM_HOOK_EVENT == "after-split-window"
    assert KAIZEN_TEAM_HOOK_NAME == "after-split-window[88]"
    cmd = build_team_fold_hook_command(**_HOOK_KW)
    # The re-entrancy analysis only holds for the pane-add event; a rebinding
    # to any layout-change event would loop (fold → event → fold → ...).
    for looping_event in ("window-layout-changed", "after-select-layout", "after-join-pane"):
        assert looping_event not in cmd
        assert looping_event not in KAIZEN_TEAM_HOOK_NAME


def test_hook_command_is_single_background_run_shell():
    """The hook value is exactly one ``run-shell -b "<sh>"`` tmux command;
    -b keeps the fold off the tmux server's main loop."""
    cmd = build_team_fold_hook_command(**_HOOK_KW)
    script = _extract_hook_script(cmd)
    # No characters that would break tmux's double-quote parse of the script.
    assert '"' not in script
    assert "\\" not in script
    assert "$" not in script
    # "Exactly one command" means tmux must find no separator it would split
    # on: no newlines anywhere, and no ';' OUTSIDE the double-quoted script
    # (the script's own ';'s are sh statement separators, quoted away from
    # tmux). Everything outside the two delimiting quotes must be exactly
    # the run-shell invocation itself.
    assert "\n" not in cmd
    outside = cmd[: cmd.index('"')] + cmd[cmd.rindex('"') + 1 :]
    assert outside == "run-shell -b "
    assert ";" not in outside


def test_hook_script_gates_on_team_id_before_any_side_effect():
    """Concern B (BLOCKER, structural half): the team-id comparison happens in
    tmux's FORMAT layer (option value never reaches sh) and must come before
    the guard toggles and the fold invocation."""
    script = _extract_hook_script(build_team_fold_hook_command(**_HOOK_KW))
    gate_pos = script.index("#{&&:")
    # The comparison is a format conditional embedding the expected id...
    assert f"#{{==:#{{{KAIZEN_TEAM_ID_OPTION}}},tid-123}}" in script
    # ...and the RAW option value is never spliced into sh quoting (a value
    # containing a quote would otherwise be shell injection at hook-fire).
    assert f"'#{{{KAIZEN_TEAM_ID_OPTION}}}'" not in script
    assert f"'#{{{KAIZEN_FOLD_GUARD_OPTION}}}'" not in script
    assert gate_pos < script.index(f"{KAIZEN_FOLD_GUARD_OPTION} 1")
    assert gate_pos < script.index("scripts.fold_workspace")


def test_hook_command_is_env_self_contained():
    """Concern C: absolute interpreter + PYTHONPATH + cwd + TMUX socket +
    orchestrator TMUX_PANE are all embedded — nothing inherited from the
    tmux server's context."""
    script = _extract_hook_script(build_team_fold_hook_command(**_HOOK_KW))
    assert "cd '/home/op/apps/kaizen'" in script
    assert "PYTHONPATH='/home/op/apps/kaizen'" in script
    assert "'/usr/bin/python3' -m scripts.fold_workspace" in script
    assert "TMUX='/tmp/tmux-1000/default,12345,3'" in script
    # The ORCHESTRATOR pane id (not #{hook_pane}): it pins tmux's
    # current-window resolution to the kaizen window AND keeps
    # _orchestrator_pane_id()'s PM-pane exclusion correct inside the fold.
    assert "TMUX_PANE='%1'" in script
    # No bare interpreter that would resolve via the server's PATH.
    assert " python3 -m" not in script


def test_hook_script_has_reentrancy_guard_around_fold():
    """Concern A (belt-and-suspenders half): guard checked (format layer),
    set before the fold, unset after — so a sequential hook→fold→hook
    re-fire no-ops. Set/unset are addressed via the ORCHESTRATOR pane
    (stable lifetime, same window) — a #{hook_pane} target could die
    mid-fold and strand the guard at 1, muting the hook for the run."""
    script = _extract_hook_script(build_team_fold_hook_command(**_HOOK_KW))
    check = script.index(f"#{{!=:#{{{KAIZEN_FOLD_GUARD_OPTION}}},1}}")
    set_on = script.index(f"set-option -w -t '%1' {KAIZEN_FOLD_GUARD_OPTION} 1")
    fold = script.index("scripts.fold_workspace")
    unset = script.index(f"set-option -wu -t '%1' {KAIZEN_FOLD_GUARD_OPTION}")
    assert check < set_on < fold < unset
    assert "#{hook_pane}" not in script


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("team_id", "tid;rm -rf /"),
        ("team_id", "tid'quote"),
        ("team_id", 'tid"quote'),
        ("team_id", "tid with space"),
        ("team_id", "tid#{format}"),
        ("team_id", "tid$HOME"),
        ("team_id", ""),
        ("orchestrator_pane_id", "not-a-pane"),
        ("kaizen_root", "relative/path"),
        ("kaizen_root", "/path/with'quote"),
        ("python_exe", "/usr/bin/py thon"),
        ("tmux_exe", "/usr/bin/tmux;evil"),
        ("tmux_env", "no-leading-slash,1,2"),
    ],
)
def test_builder_rejects_values_outside_allowlist(field, bad_value):
    """Everything interpolated into the hook crosses tmux-parse → format
    expansion → /bin/sh; anything outside the conservative charset is
    refused outright rather than escaped."""
    kwargs = dict(_HOOK_KW)
    kwargs[field] = bad_value
    with pytest.raises(ValueError) as exc:
        build_team_fold_hook_command(**kwargs)
    assert field in str(exc.value)


# ── functional sh-level tests — no-leak and no-loop FIRST (Phase 3 close) ──


def test_hook_script_does_not_leak_to_foreign_windows(tmp_path):
    """NO-LEAK (concern B, functional half): on a window without the
    @kaizen_team_id tag the option expands empty → the script must perform
    ZERO side effects — no fold spawn, not even a guard set-option."""
    log, script = _build_with_recorders(tmp_path)
    expanded = _expand_formats(script, team_opt="", guard_opt="")
    proc = _run_sh(expanded)
    assert proc.returncode == 0, proc.stderr
    assert not log.exists(), f"foreign window triggered side effects: {log.read_text()}"


def test_hook_script_does_not_leak_to_other_team_windows(tmp_path):
    """A window tagged by a DIFFERENT kaizen team must not match either."""
    log, script = _build_with_recorders(tmp_path)
    expanded = _expand_formats(script, team_opt="other-team", guard_opt="")
    proc = _run_sh(expanded)
    assert proc.returncode == 0, proc.stderr
    assert not log.exists()


def test_hook_script_no_loop_when_guard_already_set(tmp_path):
    """NO-LOOP (concern A, functional half): a re-entrant fire — guard option
    already '1' because a hook-triggered fold is in flight — must no-op
    instead of spawning another fold (which is how a loop would sustain)."""
    log, script = _build_with_recorders(tmp_path)
    expanded = _expand_formats(script, team_opt="tid-123", guard_opt="1")
    proc = _run_sh(expanded)
    assert proc.returncode == 0, proc.stderr
    assert not log.exists(), f"re-entrant fire still ran the fold: {log.read_text()}"


def test_hook_script_runs_fold_for_team_window(tmp_path):
    """On the kaizen team window the script runs guard-on → fold → guard-off,
    exactly once, with the self-contained env actually delivered."""
    log, script = _build_with_recorders(tmp_path)
    expanded = _expand_formats(script, team_opt="tid-123", guard_opt="")
    proc = _run_sh(expanded)
    assert proc.returncode == 0, proc.stderr
    lines = log.read_text().splitlines()
    assert len(lines) == 3, lines
    guard_on, fold, guard_off = lines
    # Ordering: guard set → fold → guard unset — addressed via the
    # ORCHESTRATOR pane (%1, stable for the run; -w resolves it to the shared
    # window), never the mortal freshly-split pane.
    assert guard_on == f"TMUX|set-option -w -t %1 {KAIZEN_FOLD_GUARD_OPTION} 1"
    assert guard_off == f"TMUX|set-option -wu -t %1 {KAIZEN_FOLD_GUARD_OPTION}"
    # The fold invocation: right entrypoint, right team id, exactly one spawn.
    assert fold.startswith("PY|-m scripts.fold_workspace --team-id tid-123|")
    # Concern C delivered end-to-end: env vars + cwd as embedded.
    assert "|TMUX=/tmp/tmux-1000/default,12345,3|" in fold
    assert "|TMUX_PANE=%1|" in fold
    assert f"|PYTHONPATH={tmp_path}|" in fold
    cwd = fold.rsplit("|CWD=", 1)[1]
    assert os.path.realpath(cwd) == os.path.realpath(str(tmp_path))


# ── install_team_window_hook ──────────────────────────────────────────────


@pytest.fixture()
def _hook_env(monkeypatch):
    """Place the test inside a synthetic orchestrator pane."""
    monkeypatch.setenv("TMUX_PANE", "%1")
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,12345,3")


def test_install_tags_window_then_sets_hook(monkeypatch, _hook_env):
    """Install order is load-bearing: window tag FIRST (so the hook can never
    fire ungated), then the global indexed set-hook."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _mk_proc(0)

    monkeypatch.setattr(_tmux_config.subprocess, "run", fake_run)
    assert install_team_window_hook("tid-123", tmux_exe="/usr/bin/tmux") is True
    assert len(calls) == 2
    assert calls[0] == ["tmux", "set-option", "-w", "-t", "%1", KAIZEN_TEAM_ID_OPTION, "tid-123"]
    assert calls[1][:3] == ["tmux", "set-hook", "-g"]
    assert calls[1][3] == KAIZEN_TEAM_HOOK_NAME
    assert calls[1][4].startswith('run-shell -b "')
    assert "tid-123" in calls[1][4]


def test_install_refuses_outside_tmux(monkeypatch):
    """No $TMUX_PANE / $TMUX → warn-and-refuse with ZERO tmux calls."""
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _mk_proc(0)

    monkeypatch.setattr(_tmux_config.subprocess, "run", fake_run)
    assert install_team_window_hook("tid-123") is False
    assert calls == []


def test_install_refuses_unsafe_team_id(monkeypatch, _hook_env, capsys):
    """An allowlist-violating team id is refused BEFORE any tmux write."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _mk_proc(0)

    monkeypatch.setattr(_tmux_config.subprocess, "run", fake_run)
    assert install_team_window_hook("tid'; run-shell evil", tmux_exe="/usr/bin/tmux") is False
    assert calls == []
    assert "allowlist" in capsys.readouterr().err


def test_install_skips_hook_when_window_tag_fails(monkeypatch, _hook_env):
    """If the gate tag cannot be written, the hook must NOT be installed —
    never ship an ungated (or gate-less) global hook."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if "set-option" in argv:
            return _mk_proc(1, stderr="bad option")
        return _mk_proc(0)

    monkeypatch.setattr(_tmux_config.subprocess, "run", fake_run)
    assert install_team_window_hook("tid-123", tmux_exe="/usr/bin/tmux") is False
    assert not any("set-hook" in c for c in calls)


def test_install_tolerates_no_server(monkeypatch, _hook_env):
    """House soft-failure contract: no tmux server → False, never raises."""

    def fake_run(argv, **kwargs):
        return _mk_proc(1, stderr="no server running on /tmp/tmux-1000/default")

    monkeypatch.setattr(_tmux_config.subprocess, "run", fake_run)
    assert install_team_window_hook("tid-123", tmux_exe="/usr/bin/tmux") is False


# ── remove_team_window_hook ───────────────────────────────────────────────


def test_remove_unsets_only_the_kaizen_indexed_hook(monkeypatch, _hook_env):
    """Teardown removes OUR array entry (after-split-window[88]) and only it —
    plus the window tag and any stale guard flag — leaving operator hooks at
    other indices untouched."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _mk_proc(0)

    monkeypatch.setattr(_tmux_config.subprocess, "run", fake_run)
    assert remove_team_window_hook() is True
    unhooks = [c for c in calls if "set-hook" in c]
    assert unhooks == [["tmux", "set-hook", "-gu", KAIZEN_TEAM_HOOK_NAME]]
    # The bare event name (which would nuke ALL after-split-window hooks)
    # must never be passed — only the indexed entry.
    assert not any(c[-1] == KAIZEN_TEAM_HOOK_EVENT for c in unhooks)
    option_unsets = [c for c in calls if "set-option" in c]
    assert ["tmux", "set-option", "-wu", "-t", "%1", KAIZEN_TEAM_ID_OPTION] in option_unsets
    assert ["tmux", "set-option", "-wu", "-t", "%1", KAIZEN_FOLD_GUARD_OPTION] in option_unsets


def test_remove_skips_option_unsets_without_pane(monkeypatch):
    """Outside tmux (no pane id available) only the hook unset runs — the
    success criterion — and the option unsets are skipped, not crashed."""
    monkeypatch.delenv("TMUX_PANE", raising=False)
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _mk_proc(0)

    monkeypatch.setattr(_tmux_config.subprocess, "run", fake_run)
    assert remove_team_window_hook() is True
    assert calls == [["tmux", "set-hook", "-gu", KAIZEN_TEAM_HOOK_NAME]]


def test_remove_tolerates_no_server(monkeypatch, _hook_env):
    """No server → nothing to tear down; report False, never raise."""

    def fake_run(argv, **kwargs):
        return _mk_proc(1, stderr="no server running on /tmp/tmux-1000/default")

    monkeypatch.setattr(_tmux_config.subprocess, "run", fake_run)
    assert remove_team_window_hook() is False
