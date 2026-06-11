"""Tests for scripts/cleanup_orphans.py — three-layer orphan-teammate driver.

The most important assertion in this file is the safety-gate test:
`dry_run=False` with `team_id_pattern=None` MUST raise `ValueError`
BEFORE any subprocess is invoked. We mock `subprocess.run` and `os.kill`
and assert that they were never called when the gate fires.
"""

from __future__ import annotations

import json
import sqlite3
from unittest import mock

import pytest

from scripts import cleanup_orphans as co
from scripts.bridge_db import bootstrap


@pytest.fixture
def bridge_path(tmp_path):
    p = tmp_path / ".ai" / "bridge.db"
    bootstrap(str(p))
    return p


def _seed_orphan(bridge_path, run_id: int, team_id: str) -> None:
    """Insert a team_create row with no matching team_delete — that's
    the shape `find_orphan_team_ids` looks for."""
    con = sqlite3.connect(str(bridge_path))
    try:
        con.execute(
            "INSERT INTO bridge_requests (run_id, kind, args_json, response_json, status) "
            "VALUES (?, 'team_create', ?, ?, 'ready')",
            (
                run_id,
                json.dumps({"name": f"team-{run_id}", "members": []}),
                json.dumps({"team_id": team_id}),
            ),
        )
        con.commit()
    finally:
        con.close()


# -------------------------------------------------------------------------
# Safety gate — the highest-stakes test in this suite.
# -------------------------------------------------------------------------


def test_apply_without_pattern_raises_before_any_subprocess(bridge_path):
    """The CRITICAL contract: `dry_run=False` AND `team_id_pattern=None`
    must raise `ValueError` BEFORE invoking pgrep, tmux, kill, or rm.
    """
    with (
        mock.patch("scripts.cleanup_orphans.subprocess.run") as m_run,
        mock.patch("scripts.cleanup_orphans.os.kill") as m_kill,
        mock.patch("scripts.cleanup_orphans.shutil.rmtree") as m_rmtree,
        mock.patch("scripts.cleanup_orphans.find_orphan_team_ids") as m_orphans,
    ):
        with pytest.raises(ValueError, match="refusing to apply cleanup with no pattern"):
            co.cleanup_orphans(
                team_id_pattern=None,
                dry_run=False,
                bridge_db_path=str(bridge_path),
            )
        m_run.assert_not_called()
        m_kill.assert_not_called()
        m_rmtree.assert_not_called()
        m_orphans.assert_not_called()


def test_dry_run_invokes_no_subprocess_and_no_os_kill(bridge_path):
    """Plan mode reads ONLY from the bridge DB (sweep_leaked_teams) and
    must not touch pgrep / tmux / kill / rm."""
    _seed_orphan(bridge_path, 1, "team-alpha")
    with (
        mock.patch("scripts.cleanup_orphans.subprocess.run") as m_run,
        mock.patch("scripts.cleanup_orphans.os.kill") as m_kill,
        mock.patch("scripts.cleanup_orphans.shutil.rmtree") as m_rmtree,
    ):
        report = co.cleanup_orphans(
            team_id_pattern=None,
            dry_run=True,
            bridge_db_path=str(bridge_path),
        )
        m_run.assert_not_called()
        m_kill.assert_not_called()
        m_rmtree.assert_not_called()

    assert report["mode"] == "dry-run"
    assert report["pattern"] is None
    assert report["layer3"]["team_ids"] == ["team-alpha"]
    assert report["layer1"]["pids"] == []
    assert report["layer2"]["panes"] == []


def test_dry_run_filters_layer3_by_pattern(bridge_path):
    _seed_orphan(bridge_path, 1, "team-alpha")
    _seed_orphan(bridge_path, 2, "team-beta")
    _seed_orphan(bridge_path, 3, "team-alpha-bis")

    report = co.cleanup_orphans(
        team_id_pattern="alpha",
        dry_run=True,
        bridge_db_path=str(bridge_path),
    )
    assert sorted(report["layer3"]["team_ids"]) == ["team-alpha", "team-alpha-bis"]
    assert "team-beta" not in report["layer3"]["team_ids"]


# -------------------------------------------------------------------------
# Layer 3 reuse — make sure we delegate to find_orphan_team_ids and
# don't reinvent the bridge-DB query.
# -------------------------------------------------------------------------


def test_layer3_delegates_to_find_orphan_team_ids(bridge_path, tmp_path):
    """Confirm cleanup_orphans calls sweep_leaked_teams.find_orphan_team_ids
    rather than reimplementing the bridge-DB query."""
    fake_teams_dir = tmp_path / "teams"
    fake_teams_dir.mkdir()
    (fake_teams_dir / "team-zzz").mkdir()  # so rmtree finds something

    with (
        mock.patch(
            "scripts.cleanup_orphans.find_orphan_team_ids",
            return_value=[(7, "team-zzz")],
        ) as m_orphans,
        # Stub subprocess so Layer 1/2 don't actually shell out.
        mock.patch("scripts.cleanup_orphans.subprocess.run") as m_run,
        mock.patch("scripts.cleanup_orphans.os.kill"),
    ):
        # Fake pgrep returning no procs and tmux returning no panes.
        m_run.return_value = mock.Mock(returncode=1, stdout="", stderr="")

        report = co.cleanup_orphans(
            team_id_pattern="zzz",
            dry_run=False,
            bridge_db_path=str(bridge_path),
            teams_dir=fake_teams_dir,
        )

        m_orphans.assert_called_once_with(str(bridge_path))

    assert report["mode"] == "applied"
    assert report["layer3"]["team_ids"] == ["team-zzz"]
    assert report["layer3"]["rm_results"]["team-zzz"] == "removed"
    assert not (fake_teams_dir / "team-zzz").exists()


def test_layer3_filter_excludes_non_matching_team_ids(bridge_path, tmp_path):
    fake_teams_dir = tmp_path / "teams"
    fake_teams_dir.mkdir()
    (fake_teams_dir / "team-alpha").mkdir()
    (fake_teams_dir / "team-beta").mkdir()

    with (
        mock.patch(
            "scripts.cleanup_orphans.find_orphan_team_ids",
            return_value=[(1, "team-alpha"), (2, "team-beta")],
        ),
        mock.patch("scripts.cleanup_orphans.subprocess.run") as m_run,
        mock.patch("scripts.cleanup_orphans.os.kill"),
    ):
        m_run.return_value = mock.Mock(returncode=1, stdout="", stderr="")
        report = co.cleanup_orphans(
            team_id_pattern="alpha",
            dry_run=False,
            bridge_db_path=str(bridge_path),
            teams_dir=fake_teams_dir,
        )

    assert report["layer3"]["team_ids"] == ["team-alpha"]
    assert not (fake_teams_dir / "team-alpha").exists()
    # Non-matching team must be untouched.
    assert (fake_teams_dir / "team-beta").exists()


# -------------------------------------------------------------------------
# CLI surface — argparse plumbing.
# -------------------------------------------------------------------------


def test_cli_apply_without_pattern_returns_nonzero(bridge_path, capsys):
    """`python -m scripts.cleanup_orphans --apply` (no --pattern) must
    return 2 and print the safety-gate error to stderr, without making
    any subprocess call."""
    with (
        mock.patch("scripts.cleanup_orphans.subprocess.run") as m_run,
        mock.patch("scripts.cleanup_orphans.os.kill") as m_kill,
        mock.patch("scripts.cleanup_orphans.shutil.rmtree") as m_rmtree,
    ):
        rc = co.main(["--apply", "--bridge-db", str(bridge_path)])
        m_run.assert_not_called()
        m_kill.assert_not_called()
        m_rmtree.assert_not_called()
    assert rc == 2
    captured = capsys.readouterr()
    assert "refusing to apply cleanup with no pattern" in captured.err


def test_cli_default_is_dry_run(bridge_path, capsys):
    _seed_orphan(bridge_path, 1, "team-xyz")
    with (
        mock.patch("scripts.cleanup_orphans.subprocess.run") as m_run,
        mock.patch("scripts.cleanup_orphans.os.kill") as m_kill,
    ):
        rc = co.main(["--bridge-db", str(bridge_path)])
        m_run.assert_not_called()
        m_kill.assert_not_called()
    assert rc == 0
    out = capsys.readouterr().out
    assert "mode=dry-run" in out
    assert "team-xyz" in out


# -------------------------------------------------------------------------
# Subprocess timeout guards (pgrep / tmux list-panes / tmux kill-pane).
# -------------------------------------------------------------------------


def test_pgrep_passes_10s_timeout_kwarg():
    """Iron-Law (pre-fix failure): pgrep must carry a 10.0s timeout."""
    recorded: dict = {}

    def fake_run(argv, **kwargs):
        recorded.update(kwargs)
        return mock.Mock(returncode=1, stdout="", stderr="")

    with mock.patch("scripts.cleanup_orphans.subprocess.run", side_effect=fake_run):
        assert co._pgrep_agent_processes("xyz") == []
    assert recorded.get("timeout") == 10.0


def test_pgrep_timeout_raises_runtime_error():
    """Iron-Law (pre-fix failure): a pgrep timeout must surface LOUDLY as
    RuntimeError (matching the existing non-(0,1) exit contract), not as a
    raw TimeoutExpired."""
    import subprocess

    with (
        mock.patch(
            "scripts.cleanup_orphans.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["pgrep"], timeout=10.0),
        ),
        pytest.raises(RuntimeError, match="pgrep timed out"),
    ):
        co._pgrep_agent_processes("xyz")


def test_list_panes_passes_10s_timeout_kwarg():
    recorded: dict = {}

    def fake_run(argv, **kwargs):
        recorded.update(kwargs)
        return mock.Mock(returncode=0, stdout="", stderr="")

    with mock.patch("scripts.cleanup_orphans.subprocess.run", side_effect=fake_run):
        assert co._tmux_panes_for_pids({1}) == []
    assert recorded.get("timeout") == 10.0


def test_list_panes_timeout_returns_empty_list():
    """A tmux list-panes timeout is a soft failure — same as no-server."""
    import subprocess

    with mock.patch(
        "scripts.cleanup_orphans.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["tmux"], timeout=10.0),
    ):
        assert co._tmux_panes_for_pids({1}) == []


def test_kill_pane_passes_10s_timeout_kwarg():
    recorded: dict = {}

    def fake_run(argv, **kwargs):
        recorded.update(kwargs)
        return mock.Mock(returncode=0, stdout="", stderr="")

    with mock.patch("scripts.cleanup_orphans.subprocess.run", side_effect=fake_run):
        results = co._kill_panes(["%1"])
    assert results == {"%1": "killed"}
    assert recorded.get("timeout") == 10.0


def test_kill_pane_timeout_reports_per_pane_error():
    """A kill-pane timeout is recorded per-pane, not raised."""
    import subprocess

    with mock.patch(
        "scripts.cleanup_orphans.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["tmux"], timeout=10.0),
    ):
        results = co._kill_panes(["%1"])
    assert results == {"%1": "error: tmux kill-pane timed out"}
