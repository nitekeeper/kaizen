"""Prose-grep regression guards for load-bearing SKILL.md / script-comment invariants.

These tests assert that specific literal strings remain in their canonical
files. They guard against silent drift where a future refactor removes
documented call-sites or top-of-file cross-references without updating
their counterparts.

Pattern mirrors `tests/test_dispatch_templates_byte_identity.py`:
read the file, assert the literal substring is present, fail loudly if
not. The error messages name the contract being protected so a maintainer
who breaks them knows exactly which invariant slipped.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_skill_step_3b_invokes_sweep():
    """SKILL.md Step 3b.3 must invoke the orphan-team sweep.

    Guards GAP-6 resolution (docs/kaizen/2026-05-24-bridge-smoke-3.md):
    the sweep utility exists but was not wired in until Step 3b.3 was
    added between the create-run-only step (3b.2) and the detached-spawn
    step (3b.4). Removing the invocation re-introduces the leak: an
    orphan team from a prior crashed cycle has no recovery path.
    """
    skill_path = REPO_ROOT / "skills" / "improve" / "SKILL.md"
    text = skill_path.read_text(encoding="utf-8")
    needle = "python3 -m scripts.sweep_leaked_teams --run-id"
    assert needle in text, (
        f"Expected SKILL.md to invoke the leaked-team sweep via the "
        f"literal '{needle}'. If a refactor moved the invocation or "
        f"renamed the flag, update this test AND the matching cross-"
        f"reference in scripts/sweep_leaked_teams.py + "
        f"docs/design/python-cc-tool-bridge-design.md."
    )


def test_sweep_top_of_file_comment_names_call_site():
    """scripts/sweep_leaked_teams.py must name its SKILL.md call-site.

    Guards documentation-reality drift (the exact failure mode the
    safety reviewer caught in run-24 Phase 2: the architect's draft
    comment claimed Step 1 invoked the sweep when in fact zero call-
    sites existed). The literal 'Step 3b.3' (the actual canonical call-
    site under the post-renumber SKILL.md) must appear in the first
    10 lines so a future grep keeps surfacing the true call-site.
    """
    sweep_path = REPO_ROOT / "scripts" / "sweep_leaked_teams.py"
    with sweep_path.open(encoding="utf-8") as f:
        head = "".join(next(f) for _ in range(10))
    needle = "Step 3b.3"
    assert needle in head, (
        f"Expected '{needle}' in the first 10 lines of "
        f"{sweep_path.relative_to(REPO_ROOT)}. The top-of-file comment "
        f"must name the canonical call-site so doc + code stay in sync. "
        f"Found instead:\n{head}"
    )
