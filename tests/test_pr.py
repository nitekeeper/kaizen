"""Tests for scripts/pr.py — body rendering + gh invocation + DB update."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import scripts.pr as pr_mod
from scripts.abandonment import record_abandonment
from scripts.cycle import record_cycle_abandoned, record_cycle_success
from scripts.migrate import MIGRATIONS_DIR, apply_migrations
from scripts.pr import (
    load_run_context,
    open_pr,
    open_pr_for_run,
    render_pr_body,
    update_run_pr_url,
)
from scripts.project import create_project
from scripts.run import create_run, finalize_run, get_run

# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path) -> str:
    db_path = str(tmp_path / "kaizen.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    return db_path


@pytest.fixture
def project(db) -> dict:
    return create_project(
        db,
        git_url="https://github.com/owner/repo.git",
        name="repo",
        base_branch="main",
        test_command="pytest",
        read_paths=[],
        expert_roster=[],
        language="python",
    )


def _make_run(db, project, subject="docs cleanup", cycles_requested=3):
    return create_run(
        db,
        project_id=project["id"],
        branch="kaizen/docs-cleanup-2026-05-16-1200",
        cycles_requested=cycles_requested,
        subject=subject,
    )


def _add_success_cycle(db, run_id, cycle_n, subject, sha, slug="kaizen:cycle:x"):
    return record_cycle_success(
        db,
        run_id=run_id,
        cycle_n=cycle_n,
        subject=subject,
        commit_sha=sha,
        minutes_memex_slug=slug,
        started_at="2026-05-16T12:00:00+00:00",
    )


def _add_abandoned_cycle(
    db,
    run_id,
    cycle_n,
    subject,
    phase="meeting",
    reason="no_consensus",
    detail="agents disagreed",
    slug=None,
):
    cycle = record_cycle_abandoned(
        db,
        run_id=run_id,
        cycle_n=cycle_n,
        subject=subject,
        started_at="2026-05-16T12:00:00+00:00",
    )
    if slug is None:
        slug = f"kaizen:abandonment:{run_id}-cycle-{cycle_n}"
    ab = record_abandonment(
        db,
        cycle_id=cycle["id"],
        phase_reached=phase,
        reason=reason,
        detail=detail,
        report_memex_slug=slug,
    )
    return cycle, ab


# ── load_run_context ───────────────────────────────────────────────────────


def test_load_run_context_returns_full_state(db, project):
    run = _make_run(db, project, cycles_requested=2)
    _add_success_cycle(db, run["id"], 1, "fix a", "abcdef1234567")
    _add_abandoned_cycle(db, run["id"], 2, "fix b")
    finalize_run(db, run["id"], cycles_succeeded=1, cycles_abandoned=1)

    loaded_run, loaded_project, cycles, abandonments = load_run_context(db, run["id"])
    assert loaded_run["id"] == run["id"]
    assert loaded_project["id"] == project["id"]
    assert loaded_project["git_url"] == project["git_url"]
    assert [c["cycle_n"] for c in cycles] == [1, 2]
    assert len(abandonments) == 1
    assert abandonments[0]["cycle_id"] == cycles[1]["id"]


def test_load_run_context_raises_on_missing_run(db):
    with pytest.raises(RuntimeError, match="No run"):
        load_run_context(db, 9999)


# ── render_pr_body ─────────────────────────────────────────────────────────


def test_render_pr_body_all_success(db, project):
    run = _make_run(db, project, subject="docs", cycles_requested=3)
    _add_success_cycle(db, run["id"], 1, "fix 1", "1111111aaaaaa")
    _add_success_cycle(db, run["id"], 2, "fix 2", "2222222bbbbbb")
    _add_success_cycle(db, run["id"], 3, "fix 3", "3333333cccccc")
    finalize_run(db, run["id"], cycles_succeeded=3, cycles_abandoned=0)

    loaded_run, loaded_project, cycles, abandonments = load_run_context(db, run["id"])
    title, body = render_pr_body(loaded_run, loaded_project, cycles, abandonments)
    assert "3 cycles, 3 succeeded / 0 abandoned" in title
    assert "kaizen: docs" in title
    # All three cycle sections present.
    assert "### Cycle 1 — success" in body
    assert "### Cycle 2 — success" in body
    assert "### Cycle 3 — success" in body
    # Short shas (7 chars).
    assert "`1111111`" in body
    assert "`2222222`" in body
    assert "`3333333`" in body
    # No abandonment section.
    assert "## Abandonment reports" not in body


def test_render_pr_body_mixed_outcomes(db, project):
    run = _make_run(db, project, subject="cleanup", cycles_requested=3)
    _add_success_cycle(db, run["id"], 1, "fix 1", "1111111aaaaaa")
    _add_abandoned_cycle(
        db,
        run["id"],
        2,
        "tricky one",
        phase="meeting",
        reason="no_consensus",
        detail="experts could not agree",
        slug="kaizen:abandonment:1-cycle-2",
    )
    _add_success_cycle(db, run["id"], 3, "fix 3", "3333333cccccc")
    finalize_run(db, run["id"], cycles_succeeded=2, cycles_abandoned=1)

    loaded_run, loaded_project, cycles, abandonments = load_run_context(db, run["id"])
    title, body = render_pr_body(loaded_run, loaded_project, cycles, abandonments)
    assert "3 cycles, 2 succeeded / 1 abandoned" in title
    assert "### Cycle 2 — abandoned" in body
    assert "Phase reached: meeting" in body
    assert "Reason: no_consensus" in body
    assert "## Abandonment reports" in body
    assert "`kaizen:abandonment:1-cycle-2`" in body
    # Success commit still listed.
    assert "`1111111`" in body


def test_render_pr_body_all_abandoned(db, project):
    run = _make_run(db, project, subject=None, cycles_requested=3)
    _add_abandoned_cycle(
        db,
        run["id"],
        1,
        None,
        slug="kaizen:abandonment:1-cycle-1",
    )
    _add_abandoned_cycle(
        db,
        run["id"],
        2,
        None,
        slug="kaizen:abandonment:1-cycle-2",
    )
    _add_abandoned_cycle(
        db,
        run["id"],
        3,
        None,
        slug="kaizen:abandonment:1-cycle-3",
    )
    finalize_run(db, run["id"], cycles_succeeded=0, cycles_abandoned=3)

    loaded_run, loaded_project, cycles, abandonments = load_run_context(db, run["id"])
    title, body = render_pr_body(loaded_run, loaded_project, cycles, abandonments)
    assert "0 succeeded / 3 abandoned" in title
    assert "## Abandonment reports" in body
    assert "`kaizen:abandonment:1-cycle-1`" in body
    assert "`kaizen:abandonment:1-cycle-2`" in body
    assert "`kaizen:abandonment:1-cycle-3`" in body
    # No commit shas — only the dash placeholder.
    assert "`1111111`" not in body
    assert "Commit: —" in body


def test_render_pr_body_subject_pm_directed_when_null(db, project):
    run = _make_run(db, project, subject=None, cycles_requested=1)
    _add_success_cycle(db, run["id"], 1, None, "1111111aaaaaa")
    finalize_run(db, run["id"], cycles_succeeded=1, cycles_abandoned=0)

    loaded_run, loaded_project, cycles, abandonments = load_run_context(db, run["id"])
    title, body = render_pr_body(loaded_run, loaded_project, cycles, abandonments)
    assert "kaizen: PM-directed —" in title
    assert "Subject: PM-directed" in body


def test_render_pr_body_long_detail_truncated(db, project):
    run = _make_run(db, project, subject="x", cycles_requested=1)
    long_detail = "a" * 500
    _add_abandoned_cycle(
        db,
        run["id"],
        1,
        "x",
        detail=long_detail,
        slug="kaizen:abandonment:1-cycle-1",
    )
    finalize_run(db, run["id"], cycles_succeeded=0, cycles_abandoned=1)

    loaded_run, loaded_project, cycles, abandonments = load_run_context(db, run["id"])
    _title, body = render_pr_body(loaded_run, loaded_project, cycles, abandonments)
    expected = "Detail summary: " + ("a" * 200) + "..."
    assert expected in body
    # Original 500-char string isn't dumped wholesale.
    assert ("a" * 500) not in body


def test_render_pr_body_includes_timestamps_formatted(db, project):
    run = _make_run(db, project, subject="x", cycles_requested=1)
    _add_success_cycle(db, run["id"], 1, "x", "1111111aaaaaa")
    # Stamp known timestamps directly.
    from scripts.db import get_connection

    conn = get_connection(db)
    try:
        conn.execute(
            "UPDATE runs SET started_at = ?, ended_at = ? WHERE id = ?",
            ("2026-05-16T14:23:00+00:00", "2026-05-16T15:47:00+00:00", run["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    loaded_run, loaded_project, cycles, abandonments = load_run_context(db, run["id"])
    _title, body = render_pr_body(loaded_run, loaded_project, cycles, abandonments)
    assert "Run started | 2026-05-16 14:23 UTC" in body
    assert "Run ended | 2026-05-16 15:47 UTC" in body


# ── _fmt_ts ───────────────────────────────────────────────────────────────


class TestFmtTs:
    def test_fmt_ts_naive_treated_as_utc(self):
        from scripts.pr import _fmt_ts

        assert _fmt_ts("2026-05-16T14:23:00") == "2026-05-16 14:23 UTC"

    def test_fmt_ts_with_offset_converted_to_utc(self):
        from scripts.pr import _fmt_ts

        assert _fmt_ts("2026-05-16T14:23:00+05:30") == "2026-05-16 08:53 UTC"

    def test_fmt_ts_negative_offset_converted_to_utc(self):
        from scripts.pr import _fmt_ts

        assert _fmt_ts("2026-05-16T14:23:00-04:00") == "2026-05-16 18:23 UTC"

    def test_fmt_ts_utc_offset_unchanged(self):
        from scripts.pr import _fmt_ts

        assert _fmt_ts("2026-05-16T14:23:00+00:00") == "2026-05-16 14:23 UTC"

    def test_fmt_ts_returns_em_dash_for_none(self):
        from scripts.pr import _fmt_ts

        assert _fmt_ts(None) == "—"

    def test_fmt_ts_returns_em_dash_for_empty_string(self):
        from scripts.pr import _fmt_ts

        assert _fmt_ts("") == "—"

    def test_fmt_ts_returns_raw_for_invalid_iso(self):
        from scripts.pr import _fmt_ts

        assert _fmt_ts("not-a-timestamp") == "not-a-timestamp"

    def test_fmt_ts_z_suffix_treated_as_utc(self):
        from scripts.pr import _fmt_ts

        assert _fmt_ts("2026-05-16T14:23:00Z") == "2026-05-16 14:23 UTC"


def test_render_pr_body_counts_from_cycles_when_run_counters_still_zero(db, project):
    """Regression: render_pr_body must compute succeeded/abandoned from the cycles list,
    not from runs.cycles_succeeded / cycles_abandoned. In the production order
    (internal/run/SKILL.md), open-PR runs BEFORE finalize_run, so the run row's
    counters are still 0 when render_pr_body is called.
    """
    from scripts.cycle import record_cycle_abandoned, record_cycle_success
    from scripts.run import create_run

    run = create_run(
        db, project_id=project["id"], branch="kaizen/test", cycles_requested=3, subject=None
    )
    # 2 successes, 1 abandoned — but do NOT call finalize_run.
    record_cycle_success(
        db,
        run_id=run["id"],
        cycle_n=1,
        subject=None,
        commit_sha="abc1234",
        minutes_memex_slug=None,
        started_at="2026-05-23T00:00:00+00:00",
    )
    record_cycle_success(
        db,
        run_id=run["id"],
        cycle_n=2,
        subject=None,
        commit_sha="def5678",
        minutes_memex_slug=None,
        started_at="2026-05-23T00:01:00+00:00",
    )
    record_cycle_abandoned(
        db, run_id=run["id"], cycle_n=3, subject=None, started_at="2026-05-23T00:02:00+00:00"
    )

    run_row, project_row, cycles, abandonments = load_run_context(db, run["id"])
    # Confirm the precondition: run-row counters are still zero.
    assert run_row["cycles_succeeded"] == 0
    assert run_row["cycles_abandoned"] == 0

    title, body = render_pr_body(run_row, project_row, cycles, abandonments)

    # The title and Summary table must reflect the truth (computed from cycles), not the
    # stale run-row counters.
    assert "2 succeeded / 1 abandoned" in title
    assert "| Succeeded | 2 |" in body
    assert "| Abandoned | 1 |" in body


# ── open_pr (subprocess mocked) ────────────────────────────────────────────


def test_open_pr_invokes_gh_with_correct_args(tmp_path, monkeypatch):
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir()
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["cwd"] = kwargs.get("cwd")
        return SimpleNamespace(
            returncode=0,
            stdout="https://github.com/owner/repo/pull/7\n",
            stderr="",
        )

    monkeypatch.setattr(pr_mod.subprocess, "run", fake_run)

    url = open_pr(
        clone_dir=clone_dir,
        title="kaizen: x — 1 cycles, 1 succeeded / 0 abandoned",
        body="body content",
        base_branch="main",
        head_branch="kaizen/x-2026-05-16-1200",
    )
    assert url == "https://github.com/owner/repo/pull/7"
    cmd = captured["cmd"]
    assert cmd[0] == "gh"
    assert cmd[1] == "pr"
    assert cmd[2] == "create"
    assert "--title" in cmd
    assert "kaizen: x — 1 cycles, 1 succeeded / 0 abandoned" in cmd
    assert "--body" in cmd
    assert "body content" in cmd
    assert "--base" in cmd
    assert "main" in cmd
    assert "--head" in cmd
    assert "kaizen/x-2026-05-16-1200" in cmd
    assert captured["cwd"] == str(clone_dir)


def test_open_pr_returns_url_from_gh_output(tmp_path, monkeypatch):
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir()

    def fake_run(cmd, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout="some noise line\nhttps://github.com/owner/repo/pull/42\n",
            stderr="",
        )

    monkeypatch.setattr(pr_mod.subprocess, "run", fake_run)

    url = open_pr(clone_dir, "t", "b", "main", "k")
    assert url == "https://github.com/owner/repo/pull/42"


def test_open_pr_raises_on_gh_failure(tmp_path, monkeypatch):
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir()

    def fake_run(cmd, **kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="gh: not authenticated. Run `gh auth login`.\n",
        )

    monkeypatch.setattr(pr_mod.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError) as exc_info:
        open_pr(clone_dir, "t", "b", "main", "k")
    assert "not authenticated" in str(exc_info.value)


# ── update_run_pr_url ──────────────────────────────────────────────────────


def test_update_run_pr_url_persists(db, project):
    run = _make_run(db, project)
    update_run_pr_url(db, run["id"], "https://github.com/owner/repo/pull/9")
    reloaded = get_run(db, run["id"])
    assert reloaded["pr_url"] == "https://github.com/owner/repo/pull/9"


# ── open_pr_for_run (full flow with mocked gh) ─────────────────────────────


def test_open_pr_for_run_full_flow(db, project, tmp_path, monkeypatch):
    run = _make_run(db, project, subject="docs", cycles_requested=2)
    _add_success_cycle(db, run["id"], 1, "fix 1", "1111111aaaaaa")
    _add_abandoned_cycle(
        db,
        run["id"],
        2,
        "fix 2",
        slug="kaizen:abandonment:1-cycle-2",
    )
    finalize_run(db, run["id"], cycles_succeeded=1, cycles_abandoned=1)

    clone_dir = tmp_path / "clone"
    clone_dir.mkdir()

    def fake_run(cmd, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout="https://github.com/owner/repo/pull/123\n",
            stderr="",
        )

    monkeypatch.setattr(pr_mod.subprocess, "run", fake_run)

    url = open_pr_for_run(db, run["id"], clone_dir)
    assert url == "https://github.com/owner/repo/pull/123"
    final = get_run(db, run["id"])
    assert final["pr_url"] == "https://github.com/owner/repo/pull/123"
