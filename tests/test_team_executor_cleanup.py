"""Tests for kaizen#68 — OS-level teammate cleanup trifecta.

These tests pin the four-layer belt-and-suspenders cleanup performed by
`scripts.team_executor._cleanup_team_artifacts` and
`_cleanup_verify_config_dir`. The bug being fixed: after a kaizen-on-
kaizen cycle ends, the spawned teammate `claude --agent-id <name>@<team>`
processes and their tmux panes survive indefinitely because CC's
`TeamDelete` only cleans the session-scoped team registry — it does NOT
SIGTERM/SIGKILL the spawned processes, and tmux panes hosting those
processes don't auto-close because the child is still alive.

# Iron Law (kaizen CLAUDE.md F11)

Each test exercises a code path that does not exist on `main` (pre-fix):
the cleanup helper itself, layer-by-layer. The shutdown_request
handshake is left untouched (that's a known CC limitation per the issue
body); these tests pin the NEW OS-level layers.

# Test harness

We mock `subprocess.run` at the module boundary by monkeypatching the
six seam functions defined in `scripts.team_executor`:

  - `_pgrep_teammates(team_name) -> list[int]`
  - `_pkill_teammates(team_name, signal)`
  - `_tmux_list_panes() -> list[tuple[str, int, str, str]]` — iter 3
    added the trailing `@kaizen_team_id` field for cross-team safety.
  - `_ps_args(pid) -> str`
  - `_tmux_kill_pane(pane_id) -> bool`
  - `_sleep(seconds)`  (made into a no-op)

This avoids spawning real `claude`/`pkill`/`tmux`/`pgrep` processes
under pytest. A fake-Agent harness would be premature complexity — the
seams above ARE the integration contract this cleanup helper relies on.
"""

from __future__ import annotations

import contextlib
import os
from unittest.mock import patch

import pytest

import scripts.team_executor as tx

# ── Autouse fixtures ──────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_missing_tool_registry(monkeypatch):
    """Reset the module-level one-shot warning registry per test.

    kaizen#68 iter 3 NIT — make per-test isolation EXPLICIT rather than
    relying on individual tests to monkeypatch it. Without this, the
    first test that triggers a missing-tool warning would suppress all
    subsequent tests' warnings even when the test order changes.
    """
    monkeypatch.setattr(tx, "_MISSING_TOOL_WARNED", set())


# ── Fake-Agent harness ────────────────────────────────────────────────────


_DEFAULT_TEAM_ID = "team-abc-123"
_DEFAULT_ROSTER = ["pm-1", "backend-engineer-1", "security-engineer-1"]


def _pane(
    pane_id: str,
    pane_pid: int,
    pane_title: str,
    kaizen_team_id: str = _DEFAULT_TEAM_ID,
) -> tuple[str, int, str, str]:
    """Build a 4-tuple pane fixture with a default kaizen_team_id.

    iter 3: the cleanup harness uses 4-tuples (last field is the
    `@kaizen_team_id` user-option). Tests that don't care about the
    cross-team safety gate get the default team_id so they pass the
    L3 PRIMARY filter; tests that DO care override per-pane.
    """
    return (pane_id, pane_pid, pane_title, kaizen_team_id)


class _SubprocessHarness:
    """Records subprocess seam calls and returns scripted responses.

    Each public attribute is a list of recorded events; the corresponding
    side-effect maps live in `pgrep_responses`, `panes`, `ps_args_map`,
    etc. Construction defaults are "happy path": no survivors, no panes,
    no config dir.
    """

    def __init__(
        self,
        *,
        pgrep_responses: list[list[int]] | None = None,
        panes: list[tuple[str, int, str, str]] | None = None,
        ps_args_map: dict[int, str] | None = None,
        kill_pane_succeeds: dict[str, bool] | None = None,
    ):
        # pgrep returns scripted values in order — first call gets [0],
        # second [1], etc. Default empty list (no survivors).
        self.pgrep_responses = list(pgrep_responses or [[]])
        self.pgrep_calls: list[str] = []
        self.pkill_calls: list[tuple[str, str]] = []
        # iter 3: panes are 4-tuples (pane_id, pane_pid, pane_title, kaizen_team_id)
        self.panes = list(panes or [])
        self.ps_args_map = dict(ps_args_map or {})
        self.ps_calls: list[int] = []
        self.kill_pane_calls: list[str] = []
        self.kill_pane_succeeds = dict(kill_pane_succeeds or {})
        self.sleep_calls: list[float] = []
        self._pgrep_idx = 0

    def pgrep(self, team_name: str) -> list[int]:
        self.pgrep_calls.append(team_name)
        if self._pgrep_idx < len(self.pgrep_responses):
            out = self.pgrep_responses[self._pgrep_idx]
            self._pgrep_idx += 1
            return list(out)
        # Beyond scripted: empty (happy default — assume kill worked).
        return []

    def pkill(self, team_name: str, signal: str) -> None:
        self.pkill_calls.append((team_name, signal))

    def list_panes(self) -> list[tuple[str, int, str, str]]:
        return list(self.panes)

    def ps_args(self, pid: int) -> str:
        self.ps_calls.append(pid)
        return self.ps_args_map.get(pid, "")

    def kill_pane(self, pane_id: str) -> bool:
        self.kill_pane_calls.append(pane_id)
        return self.kill_pane_succeeds.get(pane_id, True)

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)


def _install_harness(monkeypatch, harness: _SubprocessHarness) -> None:
    """Wire the harness into team_executor's module seams."""
    monkeypatch.setattr(tx, "_pgrep_teammates", harness.pgrep)
    monkeypatch.setattr(tx, "_pkill_teammates", harness.pkill)
    monkeypatch.setattr(tx, "_tmux_list_panes", harness.list_panes)
    monkeypatch.setattr(tx, "_ps_args", harness.ps_args)
    monkeypatch.setattr(tx, "_tmux_kill_pane", harness.kill_pane)
    monkeypatch.setattr(tx, "_sleep", harness.sleep)


# ── Test 1: happy path — shutdown_response succeeded, all gone ────────────


def test_happy_path_no_survivors(monkeypatch, tmp_path):
    """L1 reports 0 survivors → L2 + L3 are no-ops → L4 happy.

    Layer pinned: L1 + early-exit invariant. Iron Law: this test
    requires `_cleanup_team_artifacts` to exist, which it does not on
    `main`.
    """
    harness = _SubprocessHarness()
    _install_harness(monkeypatch, harness)
    monkeypatch.setattr(tx, "_team_config_dir", lambda tid: tmp_path / "absent" / tid)

    report = tx._cleanup_team_artifacts(
        "kaizen-cycle-1-1",
        team_id=_DEFAULT_TEAM_ID,
        team_role_ids=_DEFAULT_ROSTER,
        shutdown_was_attempted=True,
    )

    assert report["l1_survivors"] == 0
    assert report["l2_sigterm_sent"] == 0
    assert report["l2_sigkill_needed"] == 0
    assert report["l3_panes_killed"] == 0
    assert harness.pkill_calls == []
    assert len(harness.pgrep_calls) == 1
    # L4 — separate invocation, config dir absent → no fallback fired.
    # Use a UUID-shaped team_id so the L4 leaf-match guard accepts it.
    fallback_fired = tx._cleanup_verify_config_dir("team-uuid-1")
    assert fallback_fired is False


# ── Test 2: survivors exist but SIGTERM works → no SIGKILL needed ─────────


def test_survivors_die_on_sigterm(monkeypatch, tmp_path):
    """L1 detects survivors → L2 fires SIGTERM → re-pgrep is empty → no
    SIGKILL. L3 kills the corresponding tmux panes via team_id gate.

    Layers pinned: L1 + L2 (SIGTERM path) + L3 (team_id-gated kill).
    """
    harness = _SubprocessHarness(
        pgrep_responses=[[100, 200], []],
        panes=[
            _pane("%5", 100, "backend-engineer-1"),
            _pane("%6", 200, "security-engineer-1"),
            # %7 is unrelated — different team_id.
            _pane("%7", 999, "some-title", kaizen_team_id="other-team"),
        ],
    )
    _install_harness(monkeypatch, harness)
    monkeypatch.setattr(tx, "_team_config_dir", lambda tid: tmp_path / "absent" / tid)
    with patch.dict(
        os.environ, {k: v for k, v in os.environ.items() if k != "TMUX_PANE"}, clear=True
    ):
        report = tx._cleanup_team_artifacts(
            "kaizen-cycle-2-1",
            team_id=_DEFAULT_TEAM_ID,
            team_role_ids=_DEFAULT_ROSTER,
            shutdown_was_attempted=True,
        )

    assert report["l1_survivors"] == 2
    assert report["l2_sigterm_sent"] == 2
    assert report["l2_sigkill_needed"] == 0
    signals = [sig for _, sig in harness.pkill_calls]
    assert "-TERM" in signals
    assert "-KILL" not in signals
    # L3 (team_id-gated): %5 + %6 share our team_id and are killed;
    # %7 carries a different team_id and is explicitly skipped.
    assert sorted(harness.kill_pane_calls) == ["%5", "%6"]
    assert report["l3_panes_killed"] == 2
    assert report["l3_panes_skipped_other_team"] == 1


# ── Test 3: SIGTERM doesn't take → escalate to SIGKILL ────────────────────


def test_sigterm_doesnt_take_escalate_to_sigkill(monkeypatch, tmp_path):
    """L1 detects survivors → L2 SIGTERM → re-pgrep STILL has survivors →
    SIGKILL fires. L3 still kills panes via the team_id gate.

    Layer pinned: L2 full escalation path.
    """
    harness = _SubprocessHarness(
        pgrep_responses=[[100, 200], [100, 200]],
        panes=[
            _pane("%5", 100, "backend-engineer-1"),
            _pane("%6", 200, "security-engineer-1"),
        ],
    )
    _install_harness(monkeypatch, harness)
    monkeypatch.setattr(tx, "_team_config_dir", lambda tid: tmp_path / "absent" / tid)
    with patch.dict(
        os.environ, {k: v for k, v in os.environ.items() if k != "TMUX_PANE"}, clear=True
    ):
        report = tx._cleanup_team_artifacts(
            "kaizen-cycle-3-1",
            team_id=_DEFAULT_TEAM_ID,
            team_role_ids=_DEFAULT_ROSTER,
            shutdown_was_attempted=True,
        )

    assert report["l1_survivors"] == 2
    assert report["l2_sigterm_sent"] == 2
    assert report["l2_sigkill_needed"] == 2
    signals = [sig for _, sig in harness.pkill_calls]
    assert signals == ["-TERM", "-KILL"]
    assert sorted(harness.kill_pane_calls) == ["%5", "%6"]
    assert report["l3_panes_killed"] == 2


# ── Test 4: orchestrator-pane exclusion ───────────────────────────────────


def test_orchestrator_pane_is_never_killed(monkeypatch, tmp_path):
    """Even if the orchestrator's TMUX_PANE pid somehow matched a
    teammate process AND its title matched a role-id AND it carried a
    team_id tag (the truly pathological case after iter-3 fix — every
    match vector collides on the orchestrator pane), L3 MUST NOT kill
    the orchestrator's pane.

    Layer pinned: L3 orchestrator-pane exclusion.
    """
    harness = _SubprocessHarness(
        pgrep_responses=[[100], []],
        panes=[
            # %3 is orchestrator with everything that would qualify it.
            _pane("%3", 100, "backend-engineer-1"),
            # %5 is a real teammate pane.
            _pane("%5", 100, "backend-engineer-1"),
        ],
    )
    _install_harness(monkeypatch, harness)
    monkeypatch.setattr(tx, "_team_config_dir", lambda tid: tmp_path / "absent" / tid)
    with patch.dict(os.environ, {"TMUX_PANE": "%3"}):
        report = tx._cleanup_team_artifacts(
            "kaizen-cycle-4-1",
            team_id=_DEFAULT_TEAM_ID,
            team_role_ids=_DEFAULT_ROSTER,
            shutdown_was_attempted=True,
        )

    assert "%3" not in harness.kill_pane_calls
    assert harness.kill_pane_calls == ["%5"]
    assert report["l3_panes_skipped_orchestrator"] == 1
    assert report["l3_panes_killed"] == 1


# ── Test 5: tmux server unavailable ───────────────────────────────────────


def test_tmux_unavailable_skips_layer_3(monkeypatch, tmp_path):
    """When tmux returns no panes, L3 is a no-op; L1/L2/L4 still work.
    L3 still emits its summary line so the operator can distinguish
    "tmux unavailable" from "0 panes killed" (MINOR fix in iter 2).
    """
    harness = _SubprocessHarness(
        pgrep_responses=[[100], []],
        panes=[],
    )
    _install_harness(monkeypatch, harness)
    monkeypatch.setattr(tx, "_team_config_dir", lambda tid: tmp_path / "absent" / tid)
    with patch.dict(
        os.environ, {k: v for k, v in os.environ.items() if k != "TMUX_PANE"}, clear=True
    ):
        report = tx._cleanup_team_artifacts(
            "kaizen-cycle-5-1",
            team_id=_DEFAULT_TEAM_ID,
            team_role_ids=_DEFAULT_ROSTER,
            shutdown_was_attempted=True,
        )

    assert report["l1_survivors"] == 1
    assert report["l2_sigterm_sent"] == 1
    assert report["l3_panes_killed"] == 0
    assert harness.kill_pane_calls == []
    assert report["l3_tmux_available"] is False


# ── Test 6: idempotency ─────────────────────────────────────────────────


def test_idempotent_double_call(monkeypatch, tmp_path):
    """Calling the cleanup helper twice: second call is a no-op."""
    harness = _SubprocessHarness(
        pgrep_responses=[[100], [], []],
        panes=[_pane("%5", 100, "backend-engineer-1")],
    )
    _install_harness(monkeypatch, harness)
    monkeypatch.setattr(tx, "_team_config_dir", lambda tid: tmp_path / "absent" / tid)
    with patch.dict(
        os.environ, {k: v for k, v in os.environ.items() if k != "TMUX_PANE"}, clear=True
    ):
        report_1 = tx._cleanup_team_artifacts(
            "kaizen-cycle-6-1",
            team_id=_DEFAULT_TEAM_ID,
            team_role_ids=_DEFAULT_ROSTER,
            shutdown_was_attempted=True,
        )
        harness.panes = []
        report_2 = tx._cleanup_team_artifacts(
            "kaizen-cycle-6-1",
            team_id=_DEFAULT_TEAM_ID,
            team_role_ids=_DEFAULT_ROSTER,
            shutdown_was_attempted=True,
        )

    assert report_1["l1_survivors"] == 1
    assert report_1["l3_panes_killed"] == 1
    assert report_2["l1_survivors"] == 0
    assert report_2["l2_sigterm_sent"] == 0
    assert report_2["l3_panes_killed"] == 0
    f1 = tx._cleanup_verify_config_dir("team-uuid-6")
    f2 = tx._cleanup_verify_config_dir("team-uuid-6")
    assert f1 is False
    assert f2 is False


# ── Test 7: cross-team safety via pkill regex anchoring ─────────────────


def test_pkill_regex_safety_cross_team(monkeypatch, tmp_path):
    """The pkill regex MUST be anchored on a literal-space-or-EOL
    boundary after team_name so ``kaizen-cycle-5-1`` does not match
    ``kaizen-cycle-5-11``. NIT (iter 2): ``( |$)`` rather than ``(\\s|$)``.
    """
    import re as _re

    rgx = _re.compile(tx._agent_id_regex("kaizen-cycle-5-1"))

    assert rgx.search("/usr/bin/claude --agent-id backend-engineer-1@kaizen-cycle-5-1 --foo")
    assert rgx.search("/usr/bin/claude --agent-id pm-1@kaizen-cycle-5-1")
    assert not rgx.search("/usr/bin/claude --agent-id backend-engineer-1@kaizen-cycle-5-11 --foo")
    assert not rgx.search("/usr/bin/claude --agent-id backend-engineer-1@kaizen-cycle-99-2 --foo")
    assert tx._agent_id_regex("x") == r"--agent-id \S+@x( |$)"


# ── Test 8 (iter 3 MAJOR): L3 cross-team safety via @kaizen_team_id tag ──


def test_l3_cross_team_safety_via_team_id_tag(monkeypatch, tmp_path):
    """L3 PRIMARY gate must use the per-pane ``@kaizen_team_id`` tag
    so a concurrent kaizen orchestrator's panes (sharing role-ids) are
    NEVER killed.

    Scenario: two simulated kaizen orchestrators running concurrently.
    Both teams have overlapping role-ids ("arch-1", "be-1"). We call
    cleanup for team A. Team B's panes — same titles, different
    ``@kaizen_team_id`` — must survive.

    iter 3 MAJOR fix. Iron Law: this test fails on iter-2 (L3 matched
    by title alone and would kill team B's panes too).
    """
    team_a = "team-aaa-aaa"
    team_b = "team-bbb-bbb"
    roster = ["arch-1", "be-1"]
    harness = _SubprocessHarness(
        pgrep_responses=[[]],  # no surviving processes
        panes=[
            # Team A panes — tagged with team_a, identical role-id titles.
            _pane("%10", 1010, "arch-1", kaizen_team_id=team_a),
            _pane("%11", 1011, "be-1", kaizen_team_id=team_a),
            # Team B panes — same role-id titles, DIFFERENT team_id.
            _pane("%20", 2020, "arch-1", kaizen_team_id=team_b),
            _pane("%21", 2021, "be-1", kaizen_team_id=team_b),
        ],
    )
    _install_harness(monkeypatch, harness)
    monkeypatch.setattr(tx, "_team_config_dir", lambda tid: tmp_path / "absent" / tid)
    with patch.dict(
        os.environ, {k: v for k, v in os.environ.items() if k != "TMUX_PANE"}, clear=True
    ):
        report = tx._cleanup_team_artifacts(
            "kaizen-cycle-team-a",
            team_id=team_a,
            team_role_ids=roster,
            shutdown_was_attempted=True,
        )

    # Team A's panes killed.
    assert sorted(harness.kill_pane_calls) == ["%10", "%11"]
    # Team B's panes UNTOUCHED.
    assert "%20" not in harness.kill_pane_calls
    assert "%21" not in harness.kill_pane_calls
    assert report["l3_panes_killed"] == 2
    assert report["l3_panes_skipped_other_team"] == 2


# ── L4 sanity / safety guards ────────────────────────────────────────────


def test_layer_4_fallback_removes_stale_config_dir(monkeypatch, tmp_path):
    """L4 verifies ~/.claude/teams/<team_id>/ post-team_delete and falls
    back to shutil.rmtree if the dir is still present. The key is
    team_id (UUID), NOT team_name (MAJOR-2 fix from iter 2).
    """
    team_id = "0f8fad5b-d9cb-469f-a165-70867728950e"
    cfg_dir = tmp_path / "fake-home" / ".claude" / "teams" / team_id
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "manifest.json").write_text("{}")
    monkeypatch.setattr(
        tx, "_team_config_dir", lambda tid: cfg_dir if tid == team_id else tmp_path / "wrong"
    )

    assert cfg_dir.exists()
    fallback_fired = tx._cleanup_verify_config_dir(team_id)
    assert fallback_fired is True
    assert not cfg_dir.exists()


def test_team_config_dir_uses_team_id_not_team_name():
    """`_team_config_dir(team_id)` must return ``~/.claude/teams/<team_id>``."""
    import pathlib as _pathlib

    team_id = "0f8fad5b-d9cb-469f-a165-70867728950e"
    result = tx._team_config_dir(team_id)
    expected = _pathlib.Path.home() / ".claude" / "teams" / team_id
    assert result == expected
    assert "kaizen-cycle" not in str(result)


def test_l4_refuses_empty_team_id(monkeypatch, tmp_path):
    """L4 MUST refuse an empty team_id — would resolve to ~/.claude/teams
    (no leaf) and naive rmtree would wipe ALL kaizen team configs.

    iter 3 MINOR fix. Catastrophic-rmtree defense in depth.
    """
    # Force _team_config_dir to expose the empty-leaf path — if the
    # guard fires upstream we never reach this lambda.
    teams_root = tmp_path / "teams-root"
    teams_root.mkdir()
    (teams_root / "should-survive").mkdir()
    monkeypatch.setattr(tx, "_team_config_dir", lambda tid: teams_root)

    fallback_fired = tx._cleanup_verify_config_dir("")
    assert fallback_fired is False
    # The teams root is intact.
    assert teams_root.exists()
    assert (teams_root / "should-survive").exists()


def test_l4_refuses_path_traversal_team_id(monkeypatch, tmp_path):
    """L4 MUST refuse a team_id containing path separators or `..`.

    iter 3 MINOR fix. Unreachable from kaizen callers (team_id always
    comes from CC's TeamCreate response) but cheap defense in depth.
    """
    # Build a "victim" path to prove no rmtree happens.
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "important.txt").write_text("DO NOT DELETE")
    monkeypatch.setattr(tx, "_team_config_dir", lambda tid: victim)

    for bad in ("../foo", "a/b", "..", "foo/../bar", "a\\b"):
        fallback_fired = tx._cleanup_verify_config_dir(bad)
        assert fallback_fired is False, (
            f"L4 must refuse path-shaped team_id {bad!r}, but rmtree fired"
        )
    assert (victim / "important.txt").exists()


def test_l4_refuses_leaf_mismatch(monkeypatch, tmp_path):
    """L4 MUST refuse if _team_config_dir's resolved leaf doesn't match
    team_id. Defense in depth in case the helper was monkeypatched to
    point at an unrelated path.
    """
    decoy = tmp_path / "completely-different-name"
    decoy.mkdir()
    (decoy / "important.txt").write_text("DO NOT DELETE")
    monkeypatch.setattr(tx, "_team_config_dir", lambda tid: decoy)

    fallback_fired = tx._cleanup_verify_config_dir("the-real-team-id")
    assert fallback_fired is False
    assert (decoy / "important.txt").exists()


# ── L3 shell-wrap test — MAJOR-1 regression pin (iter 2) ────────────────


def test_l3_kills_shell_wrapped_teammate_panes(monkeypatch, tmp_path):
    """L3 must catch panes whose pane_pid is a shell wrapping claude
    (kaizen#68 empirical evidence). With iter 3 the PRIMARY match is
    the team_id tag; title is secondary defense. Both vectors hit here.
    """
    harness = _SubprocessHarness(
        pgrep_responses=[[]],
        panes=[
            # pane_pid is the bash shell (999), claude (1000) already
            # exited. Title is the role-id pinned by @desired_title.
            # Tagged with our team_id, so L3 PRIMARY catches it.
            _pane("%23", 999, "backend-engineer-1"),
            # Unrelated pane — title is the post-OSC-2-strip default,
            # AND tagged by a different team. Must NOT match.
            _pane("%50", 888, "general-purpose", kaizen_team_id="some-other-team"),
        ],
        ps_args_map={
            999: "/bin/bash",
            888: "/bin/bash",
        },
    )
    _install_harness(monkeypatch, harness)
    monkeypatch.setattr(tx, "_team_config_dir", lambda tid: tmp_path / "absent" / tid)
    with patch.dict(
        os.environ, {k: v for k, v in os.environ.items() if k != "TMUX_PANE"}, clear=True
    ):
        report = tx._cleanup_team_artifacts(
            "kaizen-cycle-7-1",
            team_id=_DEFAULT_TEAM_ID,
            team_role_ids=["backend-engineer-1", "pm-1"],
            shutdown_was_attempted=True,
        )

    assert harness.kill_pane_calls == ["%23"]
    assert report["l3_panes_killed"] == 1
    assert "%50" not in harness.kill_pane_calls


def test_l3_title_match_strips_wave_prefix(monkeypatch, tmp_path):
    """A teammate pane killed mid-wave has a title like ``[w2] sdet-1``.

    Title-match secondary path strips the prefix before comparing. iter 3
    routes through the public PANE_LABEL_PREFIX_RE in _tmux_workspace
    rather than a local duplicate.

    To exercise the SECONDARY title-match (not just the primary team_id
    gate), this test deliberately leaves the panes UNTAGGED (empty
    kaizen_team_id) — the L3 PRIMARY would skip them, and only the
    SECONDARY title-match would catch them.
    """
    harness = _SubprocessHarness(
        pgrep_responses=[[]],
        panes=[
            _pane("%30", 1234, "[w2] backend-engineer-1", kaizen_team_id=""),
            _pane("%31", 1235, "[R1] security-engineer-1", kaizen_team_id=""),
            _pane("%32", 1236, "[w10] pm-1", kaizen_team_id=""),
            # Upper-case forward-compat: PANE_LABEL_PREFIX_RE widened to [wWrR].
            _pane("%33", 1237, "[W3] backend-engineer-1", kaizen_team_id=""),
        ],
    )
    _install_harness(monkeypatch, harness)
    monkeypatch.setattr(tx, "_team_config_dir", lambda tid: tmp_path / "absent" / tid)
    with patch.dict(
        os.environ, {k: v for k, v in os.environ.items() if k != "TMUX_PANE"}, clear=True
    ):
        report = tx._cleanup_team_artifacts(
            "kaizen-cycle-7-2",
            team_id=_DEFAULT_TEAM_ID,
            team_role_ids=["backend-engineer-1", "security-engineer-1", "pm-1"],
            shutdown_was_attempted=True,
        )

    assert sorted(harness.kill_pane_calls) == ["%30", "%31", "%32", "%33"]
    assert report["l3_panes_killed"] == 4


# ── L1 grace skip when no shutdown was attempted ──────────────────────────


def test_l1_skips_grace_sleep_when_no_shutdown_attempted(monkeypatch, tmp_path):
    """MINOR (iter 2): when shutdown_was_attempted=False, L1 skips the
    2.5s grace sleep.
    """
    harness = _SubprocessHarness()
    _install_harness(monkeypatch, harness)
    monkeypatch.setattr(tx, "_team_config_dir", lambda tid: tmp_path / "absent" / tid)

    with patch.dict(
        os.environ, {k: v for k, v in os.environ.items() if k != "TMUX_PANE"}, clear=True
    ):
        tx._cleanup_team_artifacts(
            "kaizen-cycle-fast-abort",
            team_id=_DEFAULT_TEAM_ID,
            team_role_ids=_DEFAULT_ROSTER,
            shutdown_was_attempted=False,
        )

    assert tx._CLEANUP_SHUTDOWN_GRACE_S not in harness.sleep_calls


def test_l1_does_grace_sleep_when_shutdown_attempted(monkeypatch, tmp_path):
    """Inverse: L1's grace sleep IS run when shutdown was attempted."""
    harness = _SubprocessHarness()
    _install_harness(monkeypatch, harness)
    monkeypatch.setattr(tx, "_team_config_dir", lambda tid: tmp_path / "absent" / tid)

    with patch.dict(
        os.environ, {k: v for k, v in os.environ.items() if k != "TMUX_PANE"}, clear=True
    ):
        tx._cleanup_team_artifacts(
            "kaizen-cycle-normal",
            team_id=_DEFAULT_TEAM_ID,
            team_role_ids=_DEFAULT_ROSTER,
            shutdown_was_attempted=True,
        )

    assert tx._CLEANUP_SHUTDOWN_GRACE_S in harness.sleep_calls


# ── L3 summary line ──────────────────────────────────────────────────────


def test_l3_always_emits_summary_line(monkeypatch, tmp_path, capsys):
    """L3's summary line is emitted unconditionally; surfaces the
    tmux_available flag AND skipped_other_team count (iter 3).
    """
    harness = _SubprocessHarness(panes=[])
    _install_harness(monkeypatch, harness)
    monkeypatch.setattr(tx, "_team_config_dir", lambda tid: tmp_path / "absent" / tid)

    with patch.dict(
        os.environ, {k: v for k, v in os.environ.items() if k != "TMUX_PANE"}, clear=True
    ):
        tx._cleanup_team_artifacts(
            "kaizen-cycle-8-1",
            team_id=_DEFAULT_TEAM_ID,
            team_role_ids=_DEFAULT_ROSTER,
            shutdown_was_attempted=True,
        )

    err = capsys.readouterr().err
    assert "layer 3:" in err, f"L3 summary missing from stderr: {err!r}"
    assert "tmux_available=False" in err
    # iter 3: skipped_other_team is part of the summary.
    assert "skipped_other_team=" in err


# ── _tmux_list_panes parser ─────────────────────────────────────────────


def test_tmux_list_panes_parser_handles_titles_with_spaces(monkeypatch):
    """The pane_title field may contain spaces; the parser uses US
    (0x1f) as separator. iter 3: the 4th field is @kaizen_team_id.
    """
    captured: dict = {}

    class _FakeProc:
        def __init__(self, stdout, returncode=0):
            self.stdout = stdout
            self.returncode = returncode

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        sep = "\x1f"
        out = (
            f"%0{sep}315363{sep}● team-lead / PM{sep}\n"
            f"%5{sep}315845{sep}backend-engineer-1{sep}team-aaa-aaa\n"
            f"%6{sep}315900{sep}[w1] backend-engineer-1{sep}team-aaa-aaa\n"
        )
        return _FakeProc(out)

    monkeypatch.setattr(tx.subprocess, "run", fake_run)
    panes = tx._tmux_list_panes()
    assert panes == [
        ("%0", 315363, "● team-lead / PM", ""),
        ("%5", 315845, "backend-engineer-1", "team-aaa-aaa"),
        ("%6", 315900, "[w1] backend-engineer-1", "team-aaa-aaa"),
    ]
    fmt = captured["argv"][4]
    assert "\x1f" in fmt
    # iter 3: format includes the @kaizen_team_id user-option field.
    assert "@kaizen_team_id" in fmt


# ── Missing-tool warning ──────────────────────────────────────────────────


def test_pgrep_missing_emits_one_time_warning(monkeypatch, capsys):
    """When a cleanup tool is not on PATH, emit a single stderr warning
    per process. The autouse fixture above resets the registry so this
    test is order-independent.
    """

    def fake_run(*a, **kw):
        raise FileNotFoundError("[Errno 2] No such file or directory: 'pgrep'")

    monkeypatch.setattr(tx.subprocess, "run", fake_run)

    tx._pgrep_teammates("kaizen-cycle-9-1")
    tx._pgrep_teammates("kaizen-cycle-9-2")
    err = capsys.readouterr().err

    assert err.count("pgrep not on PATH") == 1, (
        f"expected exactly one missing-tool warning across two calls; got: {err!r}"
    )


# ── Wiring tests ──────────────────────────────────────────────────────────


def test_cleanup_wired_into_finally_block_before_team_delete(monkeypatch, tmp_path):
    """finally block must call `_cleanup_team_artifacts` BEFORE
    `tools.team_delete()` AND `_cleanup_verify_config_dir` AFTER, passing
    team_id (iter 3) + roster (iter 2) + shutdown flag (iter 2).
    """
    from tests.test_team_executor import MockTeamTools, _project, _run_row

    calls: list[str] = []
    cleanup_kwargs: dict = {}
    verify_args: dict = {}

    def fake_cleanup_artifacts(
        team_name, *, team_id=None, team_role_ids=None, shutdown_was_attempted=True
    ):
        calls.append(f"cleanup_artifacts({team_name})")
        cleanup_kwargs["team_id"] = team_id
        cleanup_kwargs["team_role_ids"] = team_role_ids
        cleanup_kwargs["shutdown_was_attempted"] = shutdown_was_attempted
        return {
            "team_name": team_name,
            "team_id": team_id,
            "l1_survivors": 0,
            "l2_sigterm_sent": 0,
            "l2_sigkill_needed": 0,
            "l3_panes_killed": 0,
            "l3_panes_skipped_orchestrator": 0,
            "l3_panes_skipped_other_team": 0,
            "l3_tmux_available": False,
            "l4_config_dir_cleaned_by_fallback": False,
        }

    def fake_verify_config_dir(team_id):
        calls.append(f"verify_config_dir({team_id})")
        verify_args["team_id"] = team_id
        return False

    monkeypatch.setattr(tx, "_cleanup_team_artifacts", fake_cleanup_artifacts)
    monkeypatch.setattr(tx, "_cleanup_verify_config_dir", fake_verify_config_dir)

    class _RecordingTools(MockTeamTools):
        def team_delete(self, team_id):
            calls.append(f"team_delete({team_id})")
            super().team_delete(team_id)

    roster = ["pm-1", "backend-engineer-1", "security-engineer-1"]
    tools = _RecordingTools(scripted={"Phase 1": "ABANDON: stop"})
    with patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}):
        tx.team_cycle_executor(
            clone_dir=tmp_path,
            project=_project(roster=roster),
            run_row=_run_row(),
            cycle_n=1,
            tools=tools,
        )

    cleanup_idx = next(i for i, c in enumerate(calls) if c.startswith("cleanup_artifacts"))
    delete_idx = next(i for i, c in enumerate(calls) if c.startswith("team_delete"))
    verify_idx = next(i for i, c in enumerate(calls) if c.startswith("verify_config_dir"))
    assert cleanup_idx < delete_idx < verify_idx, f"finally-block order broken: {calls}"

    assert cleanup_kwargs["team_role_ids"] == roster
    assert cleanup_kwargs["shutdown_was_attempted"] is True
    # iter 3 — team_id wired through to cleanup.
    assert cleanup_kwargs["team_id"] == "team-kaizen-cycle-1-1"
    # L4 still gets team_id.
    assert verify_args["team_id"] == "team-kaizen-cycle-1-1"


def test_cleanup_skips_shutdown_grace_on_phase_1_abandon_before_any_send(monkeypatch, tmp_path):
    """When the first send_message raises, active_members stays empty
    and the finally block must pass shutdown_was_attempted=False.
    """
    from tests.test_team_executor import MockTeamTools, _project, _run_row

    cleanup_kwargs: dict = {}

    def fake_cleanup_artifacts(
        team_name, *, team_id=None, team_role_ids=None, shutdown_was_attempted=True
    ):
        cleanup_kwargs["team_id"] = team_id
        cleanup_kwargs["team_role_ids"] = team_role_ids
        cleanup_kwargs["shutdown_was_attempted"] = shutdown_was_attempted
        return {
            "team_name": team_name,
            "team_id": team_id,
            "l1_survivors": 0,
            "l2_sigterm_sent": 0,
            "l2_sigkill_needed": 0,
            "l3_panes_killed": 0,
            "l3_panes_skipped_orchestrator": 0,
            "l3_panes_skipped_other_team": 0,
            "l3_tmux_available": False,
            "l4_config_dir_cleaned_by_fallback": False,
        }

    monkeypatch.setattr(tx, "_cleanup_team_artifacts", fake_cleanup_artifacts)
    monkeypatch.setattr(tx, "_cleanup_verify_config_dir", lambda team_id: False)

    tools = MockTeamTools(raise_on_send_call_n=1)
    with (
        patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}),
        contextlib.suppress(RuntimeError),
    ):
        tx.team_cycle_executor(
            clone_dir=tmp_path,
            project=_project(),
            run_row=_run_row(),
            cycle_n=1,
            tools=tools,
        )

    assert cleanup_kwargs["shutdown_was_attempted"] is False
    assert cleanup_kwargs["team_id"] == "team-kaizen-cycle-1-1"


# ── PANE_LABEL_PREFIX_RE — public constant import ────────────────────────


def test_pane_label_prefix_regex_is_importable_and_supports_case_insensitive():
    """NIT (iter 3): the prefix regex is exported from _tmux_workspace
    and widened to ``[wWrR]`` for forward-compat.
    """
    from scripts._tmux_workspace import PANE_LABEL_PREFIX_RE

    assert PANE_LABEL_PREFIX_RE.match("[w2] role")
    assert PANE_LABEL_PREFIX_RE.match("[W2] role")
    assert PANE_LABEL_PREFIX_RE.match("[R1] role")
    assert PANE_LABEL_PREFIX_RE.match("[r1] role")
    assert PANE_LABEL_PREFIX_RE.match("[w10] role")  # multi-digit
    # No match — bare role-id.
    assert not PANE_LABEL_PREFIX_RE.match("backend-engineer-1")
    # No match — wrong bracket shape.
    assert not PANE_LABEL_PREFIX_RE.match("(w2) role")


# ── team_delete failure must not mask the cycle exception or skip L4 ──────


def test_team_delete_failure_does_not_mask_cycle_exception_or_skip_l4(monkeypatch, tmp_path):
    """A non-BridgeError raised by `tools.team_delete` in the finally block
    must NOT replace the in-flight cycle exception, and the L4
    `_cleanup_verify_config_dir` step must still run.

    Pre-fix bug: `tools.team_delete(team_id)` was bare, so e.g. an
    sqlite3.OperationalError from the team registry replaced the original
    cycle exception and skipped L4 verification.
    """
    import sqlite3

    from tests.test_team_executor import MockTeamTools, _project, _run_row

    verify_calls: list[str] = []

    def fake_cleanup_artifacts(
        team_name, *, team_id=None, team_role_ids=None, shutdown_was_attempted=True
    ):
        return {
            "team_name": team_name,
            "team_id": team_id,
            "l1_survivors": 0,
            "l2_sigterm_sent": 0,
            "l2_sigkill_needed": 0,
            "l3_panes_killed": 0,
            "l3_panes_skipped_orchestrator": 0,
            "l3_panes_skipped_other_team": 0,
            "l3_tmux_available": False,
            "l4_config_dir_cleaned_by_fallback": False,
        }

    def fake_verify_config_dir(team_id):
        verify_calls.append(team_id)
        return False

    monkeypatch.setattr(tx, "_cleanup_team_artifacts", fake_cleanup_artifacts)
    monkeypatch.setattr(tx, "_cleanup_verify_config_dir", fake_verify_config_dir)

    class _DeleteRaisesTools(MockTeamTools):
        def team_delete(self, team_id):
            super().team_delete(team_id)  # record the call FIRST
            raise sqlite3.OperationalError("database is locked")

    # The first send_message raises RuntimeError — the ORIGINAL cycle failure.
    tools = _DeleteRaisesTools(raise_on_send_call_n=1)
    with (
        patch.dict(os.environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}),
        pytest.raises(RuntimeError, match="injected send_message failure"),
    ):
        tx.team_cycle_executor(
            clone_dir=tmp_path,
            project=_project(),
            run_row=_run_row(),
            cycle_n=1,
            tools=tools,
        )

    delete_calls = [c for c in tools.calls if c[0] == "team_delete"]
    assert delete_calls, "team_delete must still be attempted (invariant preserved)"
    assert verify_calls == ["team-kaizen-cycle-1-1"], (
        f"L4 _cleanup_verify_config_dir must run despite team_delete failure: {verify_calls}"
    )
