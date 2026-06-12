"""run-76 AI-4 — cross-module integration tests for the pane-add reconcile hook.

The per-module tests each stop at their own seam:

  * ``tests/test_tmux_config.py``    — hook command string + install/remove
    helpers (recorder stubs stand in for the fold entrypoint);
  * ``tests/test_tmux_workspace.py`` — ``fold_current_window`` settle loop +
    geometry verification (driven in-process, never via the CLI);
  * ``tests/test_team_executor.py``  — executor wiring (the helpers replaced
    by recorders).

This module encodes the END-TO-END "must-not-change" perceptual invariant
ACROSS those seams — the chain no existing test covers:

    pane-add hook fires
      → the REAL hook script (built by ``build_team_fold_hook_command``,
        format-expanded exactly the way tmux's run-shell does — reusing the
        ``_expand_formats`` shim from tests/test_tmux_config.py so the two
        files cannot drift apart)
      → executed by /bin/sh
      → spawns the REAL ``python -m scripts.fold_workspace`` CLI entrypoint
      → ``fold_current_window`` runs the reconcile loop against a faked tmux
        BINARY on $PATH (the only fake — realistic tmux 3.6b output shapes)
      → geometry verification yields a REAL True/False verdict.

No live tmux server is touched anywhere: the ``tmux`` the spawned CLI
resolves via $PATH is a recorder script, and the in-process lifecycle test
monkeypatches ``_tmux_config.subprocess.run`` (the established boundary
pattern).

The stacked-layout case is the load-bearing canary: geometry verification
that silently degraded to a ``None``-skip would make the happy-path test
indistinguishable from a broken one (both fold once), but the stacked case
would then STOP issuing its verify-retry second fold — failing loudly here.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import _tmux_config
from scripts._tmux_config import (
    _HOOK_ABS_PATH_RE,
    KAIZEN_FOLD_GUARD_OPTION,
    KAIZEN_TEAM_HOOK_NAME,
    KAIZEN_TEAM_ID_OPTION,
    build_team_fold_hook_command,
    install_team_window_hook,
    remove_team_window_hook,
)

# Reuse the canonical run-shell format-expansion shim + script extractor from
# the AI-2 test module (imported, not copied, so the simulation of tmux's
# hook-fire expansion can never drift between the unit and integration layers).
#
# ACCEPTED RESIDUAL (inherent to the no-live-server design): the expansion is
# a shared PYTHON shim, never a real tmux — if the ``#{&&:}``/``#{==:}``
# format semantics the builder relies on ever diverged from real tmux's
# format layer, no test in this repo would catch it. Mitigation: the shim
# asserts the exact gate token is present and that the expanded script is
# fully concrete (no leftover ``#{``), so any builder-vs-shim drift fails
# loudly; only a tmux-upstream semantics change slips through.
from tests.test_tmux_config import _expand_formats, _extract_hook_script

# The hook-capture test monkeypatches ``_tmux_config.subprocess.run`` — which
# IS the stdlib ``subprocess.run`` (the module object is shared) — so the real
# spawner is pinned at import time for the /bin/sh + CLI invocations below.
_REAL_RUN = subprocess.run

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TEAM_ID = "tid-123"
_TMUX_ENV = "/tmp/tmux-1000/default,12345,3"

# Realistic tmux 3.6b output shapes (same fixtures family as the _GEOM_*
# constants in tests/test_tmux_workspace.py). %1 is the orchestrator/PM pane
# (excluded from the fold via the embedded TMUX_PANE='%1'); %2..%5 are the 4
# teammates → expected grid (2, 2).
_IDS_5_PANES = "%1\n%2\n%3\n%4\n%5\n"
_GEOM_GRID_4_OK = "%1 0 0\n%2 0 100\n%3 0 150\n%4 10 100\n%5 10 150\n"
_GEOM_GRID_4_STACKED = "%1 0 0\n%2 0 100\n%3 5 100\n%4 10 100\n%5 15 100\n"

_GEOMETRY_QUERY = "list-panes -F #{pane_id} #{pane_top} #{pane_left}"
_ID_QUERY = "list-panes -F #{pane_id}"


def _require_hookable_paths() -> None:
    """The hook builder allowlists every embedded path; a checkout or
    interpreter living at an exotic path (spaces, quotes) cannot host these
    subprocess tests — skip rather than mis-report a validation refusal."""
    for name, value in (("repo root", str(_REPO_ROOT)), ("python", sys.executable)):
        if not _HOOK_ABS_PATH_RE.fullmatch(value):
            pytest.skip(f"{name} path {value!r} outside the hook-safety allowlist")


def test_environment_paths_are_hookable():
    """NON-SKIPPING CANARY for the ``_require_hookable_paths`` skips: under
    pytest.ini's ``-q --tb=short`` a skip is invisible, so a runner whose
    checkout/interpreter path fell outside the hook-safety allowlist would
    silently lose the 6 chain tests above. This test FAILS loudly in that
    environment instead — making the lost coverage a visible decision (fix
    the path, or consciously relax the allowlist), never a silent skip."""
    assert _HOOK_ABS_PATH_RE.fullmatch(str(_REPO_ROOT)), (
        f"repo root {_REPO_ROOT} fails the hook-safety allowlist "
        f"{_HOOK_ABS_PATH_RE.pattern!r} — the hook-reconcile chain tests are skipping"
    )
    assert _HOOK_ABS_PATH_RE.fullmatch(sys.executable), (
        f"python {sys.executable} fails the hook-safety allowlist "
        f"{_HOOK_ABS_PATH_RE.pattern!r} — the hook-reconcile chain tests are skipping"
    )


def _write_fake_tmux(tmp_path: Path, *, ids: str, geometry: str) -> tuple[Path, Path]:
    """Create a fake ``tmux`` binary dir + shared call log.

    The script appends every invocation to one log (preserving the relative
    order of guard toggles, folds, and reads — the cross-seam evidence) and
    answers the two list-panes queries ``fold_current_window`` issues with
    canned realistic output. Everything else (select-layout, join-pane,
    set-option) is recorded and succeeds. Stateless by design: a stable pane
    set is the scenario under test; churn is the settle-loop unit tests' job.
    """
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    log = tmp_path / "tmux-calls.log"
    fake_tmux = bin_dir / "tmux"
    fake_tmux.write_text(
        f"""#!/bin/sh
printf '%s\\n' "TMUX|$*" >> '{log}'
case "$*" in
  '{_GEOMETRY_QUERY}')
    cat <<'GEOM_EOF'
{geometry.rstrip()}
GEOM_EOF
    ;;
  '{_ID_QUERY}')
    cat <<'IDS_EOF'
{ids.rstrip()}
IDS_EOF
    ;;
esac
exit 0
"""
    )
    fake_tmux.chmod(0o755)
    return bin_dir, log


def _chain_env(bin_dir: Path) -> dict[str, str]:
    """Env for the spawned chain: fake tmux first on $PATH; no inherited
    kaizen layout knobs or real-tmux identity (the hook script embeds its
    own TMUX / TMUX_PANE / PYTHONPATH — concern C self-containment)."""
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    for var in (
        "TMUX",
        "TMUX_PANE",
        "KAIZEN_TEAMMATE_LAYOUT",
        "KAIZEN_FOLD_STABLE_MAX_ITERS",
        "KAIZEN_PM_PANE_GLYPH",
    ):
        env.pop(var, None)
    return env


def _build_production_hook_script(fake_tmux_dir: Path) -> str:
    """Build the hook command exactly as production does and peel the script."""
    cmd = build_team_fold_hook_command(
        team_id=_TEAM_ID,
        orchestrator_pane_id="%1",
        kaizen_root=str(_REPO_ROOT),
        python_exe=sys.executable,
        tmux_exe=str(fake_tmux_dir / "tmux"),
        tmux_env=_TMUX_ENV,
    )
    return _extract_hook_script(cmd)


def _fire_hook(script: str, *, team_opt: str, guard_opt: str, bin_dir: Path):
    """Simulate a hook fire: format-expand the script the way run-shell does
    for a window whose user-options hold ``team_opt`` / ``guard_opt``, then
    hand the result to /bin/sh — the production execution path."""
    expanded = _expand_formats(script, team_opt=team_opt, guard_opt=guard_opt, team_id=_TEAM_ID)
    return _REAL_RUN(
        ["/bin/sh", "-c", expanded],
        capture_output=True,
        text=True,
        timeout=120,
        env=_chain_env(bin_dir),
    )


# ── the chain: hook fire → real CLI → reconcile → verified grid ───────────


def test_hook_fire_runs_real_cli_to_verified_grid(monkeypatch, tmp_path):
    """END-TO-END HAPPY PATH: install_team_window_hook's OWN hook value (not
    a reassembled fragment) fires on the team window → /bin/sh → the real
    ``scripts.fold_workspace`` CLI → reconcile loop → geometry verification
    passes on a well-formed grid.

    Exact call-sequence pin: guard-on first, guard-off last; ONE
    reset-then-fold (settle confirmed by the second identical id read); both
    teammate pairs joined; exactly ONE geometry read and NO verify-retry
    (the True verdict). Neutering any link breaks a distinct assertion:
    hook→CLI unwired = empty log; settle loop dead = missing 2nd id read;
    kaizen#81 prepend dead = a missing/mis-paired join; verification dead =
    no geometry read.
    """
    _require_hookable_paths()
    bin_dir, log = _write_fake_tmux(tmp_path, ids=_IDS_5_PANES, geometry=_GEOM_GRID_4_OK)

    # Capture the hook value through the REAL installer (its resolution of
    # python_exe / kaizen_root is part of the chain under test).
    monkeypatch.setenv("TMUX", _TMUX_ENV)
    monkeypatch.setenv("TMUX_PANE", "%1")
    installed: list[list[str]] = []

    def fake_run(argv, **kwargs):
        installed.append(list(argv))
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_tmux_config.subprocess, "run", fake_run)
    assert install_team_window_hook(_TEAM_ID, tmux_exe=str(bin_dir / "tmux")) is True
    hook_sets = [c for c in installed if "set-hook" in c]
    assert len(hook_sets) == 1 and hook_sets[0][3] == KAIZEN_TEAM_HOOK_NAME
    script = _extract_hook_script(hook_sets[0][4])

    proc = _fire_hook(script, team_opt=_TEAM_ID, guard_opt="", bin_dir=bin_dir)
    assert proc.returncode == 0, proc.stderr

    assert log.exists(), "hook fire on the team window must reach the fold CLI"
    lines = log.read_text().splitlines()
    assert lines == [
        f"TMUX|set-option -w -t %1 {KAIZEN_FOLD_GUARD_OPTION} 1",
        f"TMUX|{_ID_QUERY}",  # settle read #1
        "TMUX|select-layout main-vertical",  # the one reset-then-fold
        "TMUX|join-pane -h -s %3 -t %2",  # pair 1 (PM %1 prepended, never folded)
        "TMUX|join-pane -h -s %5 -t %4",  # pair 2
        f"TMUX|{_ID_QUERY}",  # settle read #2 — identical → quiesced
        f"TMUX|{_GEOMETRY_QUERY}",  # verification read → True
        f"TMUX|set-option -wu -t %1 {KAIZEN_FOLD_GUARD_OPTION}",
    ], "production chain drifted; observed calls:\n" + "\n".join(lines)


def test_hook_fire_detects_half_folded_layout(tmp_path):
    """REGRESSION DETECTOR (the case this file exists for): the same chain
    against a membership-stable but STACKED window (per-pane geometry shows
    one pane per row — a half-folded grid) must yield a definite ``False``
    verdict: exactly ONE bounded verify-retry fold fires, then the chain
    gives up best-effort (exit 0, guard released).

    This is also the canary proving the happy-path test's single geometry
    read was a real ``True`` and not an unverifiable ``None``-skip: a parsing
    regression that degraded verdicts to None would skip the retry here and
    collapse the select-layout count to 1.
    """
    _require_hookable_paths()
    bin_dir, log = _write_fake_tmux(tmp_path, ids=_IDS_5_PANES, geometry=_GEOM_GRID_4_STACKED)
    script = _build_production_hook_script(bin_dir)

    proc = _fire_hook(script, team_opt=_TEAM_ID, guard_opt="", bin_dir=bin_dir)
    assert proc.returncode == 0, proc.stderr

    lines = log.read_text().splitlines()
    folds = [ln for ln in lines if ln == "TMUX|select-layout main-vertical"]
    geometry_reads = [ln for ln in lines if ln == f"TMUX|{_GEOMETRY_QUERY}"]
    assert len(folds) == 2, (
        "a stacked (half-folded) layout must trigger EXACTLY ONE verify-retry "
        f"fold (2 select-layouts total, bounded): got {len(folds)} in:\n" + "\n".join(lines)
    )
    assert len(geometry_reads) == 2, (
        f"verification must re-check after the retry (2 geometry reads); got {len(geometry_reads)}"
    )
    # Degrade-never-raise: the guard is still released — a stranded =1 guard
    # would mute every later hook fire for the rest of the run.
    assert lines[-1] == f"TMUX|set-option -wu -t %1 {KAIZEN_FOLD_GUARD_OPTION}", lines[-1]


def test_cli_entrypoint_warns_loudly_on_unmet_geometry(tmp_path):
    """The hook discards the fold's streams (``>/dev/null`` — by design), so
    the no-silent-failure half is pinned at the CLI seam: the same production
    entrypoint, run directly against the stacked window, must exit 0 (the
    best-effort contract the bridge relies on) AND emit the single greppable
    give-up warning on stderr."""
    _require_hookable_paths()
    bin_dir, _log = _write_fake_tmux(tmp_path, ids=_IDS_5_PANES, geometry=_GEOM_GRID_4_STACKED)
    env = _chain_env(bin_dir)
    env["TMUX"] = _TMUX_ENV
    env["TMUX_PANE"] = "%1"
    env["PYTHONPATH"] = str(_REPO_ROOT)
    proc = _REAL_RUN(
        [sys.executable, "-m", "scripts.fold_workspace", "--team-id", _TEAM_ID],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(_REPO_ROOT),
        env=env,
    )
    assert proc.returncode == 0, "best-effort exit-0 contract must hold even on unmet geometry"
    assert "fold geometry unmet" in proc.stderr and "after retry" in proc.stderr, proc.stderr
    assert proc.stderr.count("fold geometry unmet") == 1, "give-up warn must fire exactly once"


# ── no leak / no loop across the same chain ────────────────────────────────


@pytest.mark.parametrize(
    ("team_opt", "case"),
    [
        ("", "untagged foreign window (operator's other window)"),
        ("other-team", "window tagged by a DIFFERENT kaizen team"),
    ],
)
def test_hook_fire_on_foreign_window_makes_zero_tmux_mutations(tmp_path, team_opt, case):
    """NO-LEAK across the chain: a fire on a non-team window (option values
    as tmux would present them) must perform ZERO side effects — no guard
    set-option, no fold spawn, no tmux call of any kind."""
    _require_hookable_paths()
    bin_dir, log = _write_fake_tmux(tmp_path, ids=_IDS_5_PANES, geometry=_GEOM_GRID_4_OK)
    script = _build_production_hook_script(bin_dir)
    proc = _fire_hook(script, team_opt=team_opt, guard_opt="", bin_dir=bin_dir)
    assert proc.returncode == 0, proc.stderr
    assert not log.exists(), f"{case} mutated tmux:\n{log.read_text() if log.exists() else ''}"


def test_hook_fire_with_guard_set_skips_fold_at_hook_layer(tmp_path):
    """NO RE-ENTRANT LOOP across the chain: with ``@kaizen_fold_hook_running``
    already '1' (a hook-triggered fold in flight), a re-entrant fire must
    no-op AT THE HOOK LAYER — zero tmux calls, no second fold spawn (the
    sustaining step of a hook→fold→hook loop)."""
    _require_hookable_paths()
    bin_dir, log = _write_fake_tmux(tmp_path, ids=_IDS_5_PANES, geometry=_GEOM_GRID_4_OK)
    script = _build_production_hook_script(bin_dir)
    proc = _fire_hook(script, team_opt=_TEAM_ID, guard_opt="1", bin_dir=bin_dir)
    assert proc.returncode == 0, proc.stderr
    assert not log.exists(), f"re-entrant fire still ran: {log.read_text() if log.exists() else ''}"


# ── teardown: install + remove via the real helpers ───────────────────────


def test_install_then_remove_lifecycle_clears_hook_tag_and_guard(monkeypatch):
    """TEARDOWN across the helper pair, against a STATEFUL tmux double:
    install writes the window tag then the indexed global hook; remove unsets
    exactly that ``set-hook -gu after-split-window[88]`` entry (never the
    bare event, which would nuke operator hooks) and clears BOTH the window
    tag and a stale mid-flight guard flag. Post-remove state: no kaizen hook,
    no kaizen window options."""
    monkeypatch.setenv("TMUX", _TMUX_ENV)
    monkeypatch.setenv("TMUX_PANE", "%1")
    hooks: dict[str, str] = {"after-split-window[3]": "display-message operator-hook"}
    window_options: dict[str, str] = {}
    unhook_args: list[str] = []

    def fake_run(argv, **kwargs):
        # argv: ["tmux", subcommand, *flags/args]
        if argv[1] == "set-hook":
            if argv[2] == "-g":
                hooks[argv[3]] = argv[4]
            elif argv[2] == "-gu":
                unhook_args.append(argv[3])
                hooks.pop(argv[3], None)
        elif argv[1] == "set-option":
            if argv[2] == "-w":  # set-option -w -t <pane> <opt> <val>
                window_options[argv[5]] = argv[6]
            elif argv[2] == "-wu":  # set-option -wu -t <pane> <opt>
                window_options.pop(argv[5], None)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_tmux_config.subprocess, "run", fake_run)

    assert install_team_window_hook(_TEAM_ID, tmux_exe="/usr/bin/tmux") is True
    assert window_options.get(KAIZEN_TEAM_ID_OPTION) == _TEAM_ID, "window tag must be live"
    assert KAIZEN_TEAM_HOOK_NAME in hooks, "indexed hook must be live post-install"
    assert hooks[KAIZEN_TEAM_HOOK_NAME].startswith('run-shell -b "')

    # Simulate a fold killed mid-flight: the guard flag is stranded at 1.
    window_options[KAIZEN_FOLD_GUARD_OPTION] = "1"

    assert remove_team_window_hook() is True
    assert unhook_args == [KAIZEN_TEAM_HOOK_NAME], (
        "teardown must unset the indexed entry exactly once (and never the bare event)"
    )
    assert KAIZEN_TEAM_HOOK_NAME not in hooks, "kaizen hook must be gone post-remove"
    assert hooks == {"after-split-window[3]": "display-message operator-hook"}, (
        "operator hooks at other indices must survive kaizen teardown"
    )
    assert KAIZEN_TEAM_ID_OPTION not in window_options, "window tag must be cleared"
    assert KAIZEN_FOLD_GUARD_OPTION not in window_options, (
        "a stale mid-flight guard flag must be cleared, or every future hook "
        "fire on this window stays muted"
    )
