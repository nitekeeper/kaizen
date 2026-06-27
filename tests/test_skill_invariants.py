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


# ---------------------------------------------------------------------------
# Run-75 cycle-2 doc-parity guards (documentation/SKILL audit findings).
# Each test pins a fixed doc line so the corresponding regression goes red.
# ---------------------------------------------------------------------------

_IMPROVE_SKILL = REPO_ROOT / "skills" / "improve" / "SKILL.md"


def _doc_command_lines():
    """Yield (path, lineno, line) for every shell-invocation line of python3
    in skills/ + internal/ docs (fenced commands and inline prose commands)."""
    for base in ("skills", "internal"):
        for path in sorted((REPO_ROOT / base).rglob("*.md")):
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if "python3 scripts/" in line or re.search(r"python3 -c\s*\"?$", line):
                    yield path, i, line


def test_documented_python3_commands_carry_pythonpath():
    """Every documented `python3 scripts/...` / `python3 -c` invocation that
    imports kaizen's `scripts.*` package must set `PYTHONPATH=.` — without it
    the command fails with ModuleNotFoundError when run as documented.

    Whitelist: the stdlib-only sqlite3 snippet in expert-roster.
    """
    offenders = []
    for path, lineno, line in _doc_command_lines():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel == "internal/expert-roster/SKILL.md" and line.strip().startswith("python3 -c"):
            continue  # this snippet imports json+sqlite3 only (stdlib)
        if "PYTHONPATH=." in line:
            continue
        offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Documented python3 invocations missing PYTHONPATH=. "
        "(they import scripts.* and fail as written):\n  - " + "\n  - ".join(offenders)
    )


def test_setup_command_documented_with_pythonpath():
    text = _IMPROVE_SKILL.read_text(encoding="utf-8")
    assert "PYTHONPATH=. python3 scripts/setup.py" in text, (
        "Step 1 must document `PYTHONPATH=. python3 scripts/setup.py` — "
        "setup.py imports scripts._tmux_config and fails without it."
    )


def test_abandonment_reason_taxonomy_is_nine_codes_in_docs():
    """Docs must list the full 9-reason taxonomy from
    scripts/abandonment.py::VALID_REASONS, not the legacy 5."""
    from scripts.abandonment import VALID_REASONS

    assert len(VALID_REASONS) == 9  # if this moves, update the doc lists too
    for rel in (
        "internal/abandonment-report/SKILL.md",
        "internal/cycle/SKILL.md",
        "internal/run/SKILL.md",
    ):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        missing = sorted(r for r in VALID_REASONS if r not in text)
        assert not missing, f"{rel} is missing reason code(s): {missing}"
        assert "five named codes" not in text, f"{rel} still claims the legacy five-code taxonomy."


def test_phase_5c_minutes_not_committed_to_clone():
    """Phase 5c must NOT instruct writing minutes into the clone pre-commit:
    commit_cycle runs `git add -A`, so the minutes would land in the target
    repo's PR diff. Memex is the canonical store (Phase 5d / CLAUDE.md).

    The prose Phase 5c lives in the lazily-read `internal/cycle/prose-transport.md`
    (the KAIZEN_TRANSPORT=prose opt-out path); the default host path never reads
    it. The guard follows the content to its file."""
    text = (REPO_ROOT / "internal" / "cycle" / "prose-transport.md").read_text(encoding="utf-8")
    assert "Also write the full meeting minutes into the clone" not in text
    assert "Do **NOT** write the minutes file into the clone" in text


def test_clone_target_registration_note_matches_project_py():
    """project.py auto-detects the default branch (`_detect_base_branch` via
    `git ls-remote --symref`); the old 'hardcodes main' limitation note is
    stale and must stay gone."""
    text = (REPO_ROOT / "internal" / "clone-target" / "SKILL.md").read_text(encoding="utf-8")
    assert "hardcodes" not in text
    assert "_detect_base_branch" in text
    assert "ls-remote --symref" in text


def test_memex_run_ask_reference_form():
    """User-facing read path is the `memex:run ask` skill, not a `memex ask`
    CLI (memex is a Claude Code plugin, not a CLI binary)."""
    for base in ("skills", "internal"):
        for path in sorted((REPO_ROOT / base).rglob("*.md")):
            text = path.read_text(encoding="utf-8")
            assert re.search(r"\bmemex ask\b", text) is None, (
                f"{path.relative_to(REPO_ROOT)} references the nonexistent "
                "`memex ask` CLI form; use `memex:run ask`."
            )
