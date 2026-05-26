"""Prose-grep regression guards for load-bearing SKILL.md / script-comment invariants.

These tests assert that specific literal strings remain in their canonical
files. They guard against silent drift where a future refactor removes
documented call-sites or top-of-file cross-references without updating
their counterparts.

Pattern mirrors `tests/test_dispatch_templates_byte_identity.py`:
read the file, assert the literal substring is present, fail loudly if
not. The error messages name the contract being protected so a maintainer
who breaks them knows exactly which invariant slipped.

`test_claude_md_invariants_from_fixture` extends the same guard pattern
to a data-driven loader: it reads `tests/fixtures/claude_md_invariants.json`
(authored under kaizen#53 to enforce the portability migration of personal
rules into the repo CLAUDE.md) and evaluates each check against the named
file. The fixture, not this test, is the source of truth for what is
required to stay present (or absent) in CLAUDE.md.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "claude_md_invariants.json"


def _flag_mask(flags: str | None) -> int:
    """Convert a fixture-style flag string ('M', 'I', 'MI', '') into an `re` flag mask."""
    if not flags:
        return 0
    mask = 0
    for ch in flags:
        if ch == "M":
            mask |= re.MULTILINE
        elif ch == "I":
            mask |= re.IGNORECASE
        elif ch == "S":
            mask |= re.DOTALL
        else:
            raise ValueError(f"Unsupported regex flag character: {ch!r}")
    return mask


def _extract_section(content: str, heading: str) -> str | None:
    """Return the slice of `content` starting at the line matching `heading`
    (an H2 like '## Claude operational rules') up to the next H2 ('## ') or EOF.
    Returns None if the heading is not found.
    """
    # Match the heading at the start of a line, allow trailing whitespace.
    pattern = re.compile(
        rf"^{re.escape(heading)}\s*$",
        re.MULTILINE,
    )
    m = pattern.search(content)
    if not m:
        return None
    start = m.start()
    # Next H2 (or shallower) after the heading line.
    next_h2 = re.compile(r"^## ", re.MULTILINE)
    n = next_h2.search(content, m.end())
    end = n.start() if n else len(content)
    return content[start:end]


def _evaluate_check(check: dict, repo_root: Path) -> str | None:
    """Run a single fixture check. Return None on pass, or an error string on fail."""
    check_id = check.get("id", "<no-id>")
    check_type = check["type"]
    target_rel = check["file"]
    target = repo_root / target_rel

    if check_type == "exists":
        if not target.exists():
            return (
                f"[{check_id}] expected file to exist at {target_rel} "
                f"(absolute: {target}). Check rationale: {check.get('rationale', '<n/a>')}"
            )
        return None

    if not target.exists():
        return (
            f"[{check_id}] target file {target_rel} not found on disk; "
            f"cannot evaluate {check_type!r}."
        )

    content = target.read_text(encoding="utf-8")

    if check_type == "regex_present":
        pattern = check["pattern"]
        flags = _flag_mask(check.get("flags"))
        if re.search(pattern, content, flags) is None:
            return (
                f"[{check_id}] regex {pattern!r} (flags={check.get('flags', '')!r}) "
                f"not found in {target_rel}. Rationale: {check.get('rationale', '<n/a>')}"
            )
        return None

    if check_type == "regex_in_section":
        section_name = check["section"]
        section = _extract_section(content, section_name)
        if section is None:
            return (
                f"[{check_id}] section {section_name!r} not found in {target_rel}; "
                f"cannot evaluate regex_in_section."
            )
        pattern = check["pattern"]
        flags = _flag_mask(check.get("flags"))
        if re.search(pattern, section, flags) is None:
            return (
                f"[{check_id}] regex {pattern!r} (flags={check.get('flags', '')!r}) "
                f"not found within section {section_name!r} of {target_rel}. "
                f"Rationale: {check.get('rationale', '<n/a>')}"
            )
        return None

    if check_type == "all_keywords":
        keywords = check["keywords"]
        missing = [kw for kw in keywords if kw not in content]
        if missing:
            return (
                f"[{check_id}] missing keyword(s) in {target_rel}: {missing!r}. "
                f"Rationale: {check.get('rationale', '<n/a>')}"
            )
        return None

    if check_type == "any_of_keywords":
        keywords = check["keywords"]
        if not any(kw in content for kw in keywords):
            return (
                f"[{check_id}] none of the keywords {keywords!r} found in "
                f"{target_rel}. Rationale: {check.get('rationale', '<n/a>')}"
            )
        return None

    if check_type == "all_keywords_in_section":
        section_name = check["section"]
        section = _extract_section(content, section_name)
        if section is None:
            return (
                f"[{check_id}] section {section_name!r} not found in {target_rel}; "
                f"cannot evaluate all_keywords_in_section."
            )
        keywords = check["keywords"]
        missing = [kw for kw in keywords if kw not in section]
        if missing:
            return (
                f"[{check_id}] missing keyword(s) {missing!r} within section "
                f"{section_name!r} of {target_rel}. "
                f"Rationale: {check.get('rationale', '<n/a>')}"
            )
        any_of = check.get("any_of_keywords")
        if any_of and not any(kw in section for kw in any_of):
            return (
                f"[{check_id}] none of the any_of_keywords {any_of!r} appear "
                f"in section {section_name!r} of {target_rel}. "
                f"Rationale: {check.get('rationale', '<n/a>')}"
            )
        return None

    if check_type == "absent":
        pattern = check["pattern"]
        is_regex = bool(check.get("regex", False))
        if is_regex:
            flags = _flag_mask(check.get("flags"))
            if re.search(pattern, content, flags) is not None:
                return (
                    f"[{check_id}] forbidden regex {pattern!r} (flags="
                    f"{check.get('flags', '')!r}) found in {target_rel}. "
                    f"Rationale: {check.get('rationale', '<n/a>')}"
                )
        else:
            if pattern in content:
                return (
                    f"[{check_id}] forbidden literal {pattern!r} found in "
                    f"{target_rel}. Rationale: {check.get('rationale', '<n/a>')}"
                )
        return None

    return f"[{check_id}] unsupported check type {check_type!r}"


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


def test_claude_md_invariants_from_fixture():
    """Evaluate every check declared in `tests/fixtures/claude_md_invariants.json`.

    The fixture is the source of truth for which migrated personal rules must
    remain visible in repo CLAUDE.md (kaizen#53). This test loads the fixture,
    iterates each check, and reports every failure with the originating
    check id, the rationale from the fixture, and the missing pattern or
    keyword. Failures from this test are *intentionally* verbose so a future
    maintainer can fix the violation without having to read the fixture.

    Pre-A6 transitional behaviour: the `personal-cleanup-artifact-exists`
    check guards a file (`PERSONAL_CLEANUP_AFTER_MERGE.md`) produced by Wave 1
    A6. If that artifact is not yet on disk when this test runs (i.e. A5
    landed first), that single check is reported via `pytest.xfail` so the
    rest of the suite still surfaces real regressions. As soon as A6 lands
    the xfail naturally clears.
    """
    assert FIXTURE_PATH.exists(), (
        f"Expected fixture at {FIXTURE_PATH.relative_to(REPO_ROOT)} "
        f"(see tests/fixtures/claude_md_invariants.json)."
    )
    spec = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert spec.get("version") == 1, (
        f"Unexpected fixture version {spec.get('version')!r}; "
        f"this loader only knows about version 1."
    )
    checks = spec.get("checks", [])
    # Lock the load path: if a future refactor accidentally strips checks
    # we want a loud failure rather than a silently-empty pass.
    assert len(checks) >= 12, (
        f"Fixture should declare at least 12 invariant checks (kaizen#53 "
        f"Phase 3 consensus); found {len(checks)}."
    )

    failures: list[str] = []
    xfailed: list[str] = []
    for check in checks:
        check_id = check.get("id", "<no-id>")
        # Transitional: the personal-cleanup artifact lands in A6. If we run
        # this test before A6 finishes, surface that one check as xfail
        # rather than failing the whole test. The artifact will exist after
        # A6 and the xfail will clear automatically.
        if (
            check_id == "personal-cleanup-artifact-exists"
            and check.get("type") == "exists"
            and not (REPO_ROOT / check["file"]).exists()
        ):
            xfailed.append(
                f"[{check_id}] file {check['file']} not yet on disk (produced by Wave 1 A6)."
            )
            continue

        err = _evaluate_check(check, REPO_ROOT)
        if err:
            failures.append(err)

    if failures:
        body = "\n  - ".join(failures)
        pytest.fail(
            f"{len(failures)} invariant check(s) failed against "
            f"tests/fixtures/claude_md_invariants.json:\n  - {body}",
            pytrace=False,
        )

    if xfailed:
        # Surface but do not fail: the rest of the loader is green.
        pytest.xfail(
            "Transitional xfail for pre-A6 checks (will clear once "
            "PERSONAL_CLEANUP_AFTER_MERGE.md lands):\n  - " + "\n  - ".join(xfailed)
        )
