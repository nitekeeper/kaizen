"""Verify every SKILL.md under skills/ and internal/ has well-formed frontmatter.

Per Kaizen's convention (mirroring Atelier):
- File starts with `---\\n...---\\n` YAML frontmatter
- Frontmatter parses as valid YAML
- Frontmatter has a non-empty string `description`
- Frontmatter has NO `name` field (Claude Code derives the slash command as
  `<plugin-name>:<dir-name>` from .claude-plugin/plugin.json)
- Body after the frontmatter is non-empty

The yaml package is intentionally avoided — kaizen's requirements.txt is
stdlib-only. We parse the closing-delimiter position and the description
field with a small inline parser sufficient for the convention.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent

# Glob both public and internal skill files. Order is deterministic so test
# ids in -v output are stable.
SKILL_FILES = sorted(
    list(REPO_ROOT.glob("skills/*/SKILL.md"))
    + list(REPO_ROOT.glob("internal/*/SKILL.md"))
)


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return (frontmatter_body, post_body). Raises ValueError on bad shape."""
    # Tolerate optional BOM at start of file
    if text.startswith("﻿"):
        text = text[1:]
    if not text.startswith("---"):
        raise ValueError("file does not start with '---' frontmatter opener")
    # Normalise newlines for the split (Windows files may have CRLF)
    normalised = text.replace("\r\n", "\n")
    # Drop the first "---" line
    after_opener = normalised[3:]
    if not after_opener.startswith("\n"):
        raise ValueError("expected newline after opening '---'")
    after_opener = after_opener[1:]
    # Find the closing "---" on its own line
    m = re.search(r"^---\s*$", after_opener, flags=re.MULTILINE)
    if m is None:
        raise ValueError("no closing '---' frontmatter terminator found")
    fm_body = after_opener[: m.start()]
    post = after_opener[m.end():]
    return fm_body, post


def _parse_minimal_yaml(fm_body: str) -> dict[str, str]:
    """Parse a minimal subset: `key: value` lines, value runs to end of line.

    Sufficient for the description-only frontmatter convention. Multi-line
    values, lists, and nested keys are not supported (and not used in any
    SKILL.md we ship).
    """
    out: dict[str, str] = {}
    for raw_line in fm_body.splitlines():
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"frontmatter line has no ':': {raw_line!r}")
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        # Strip optional surrounding quotes
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key] = value
    return out


def test_skill_files_discovered() -> None:
    """Sanity: globbing finds the expected count of SKILL.md files."""
    # 1 public + 10 internal = 11
    assert len(SKILL_FILES) == 11, (
        f"Expected 11 SKILL.md files, found {len(SKILL_FILES)}: {SKILL_FILES}"
    )


@pytest.mark.parametrize("skill_file", SKILL_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)).replace("\\", "/"))
def test_skill_frontmatter(skill_file: Path) -> None:
    text = skill_file.read_text(encoding="utf-8")
    fm_body, post = _split_frontmatter(text)
    fm = _parse_minimal_yaml(fm_body)

    # description present, non-empty
    assert "description" in fm, f"{skill_file}: frontmatter missing 'description'"
    desc = fm["description"]
    assert isinstance(desc, str) and desc.strip(), (
        f"{skill_file}: 'description' must be a non-empty string"
    )

    # name MUST NOT be present (Claude Code derives slash command from
    # plugin.json + directory name; an explicit `name:` violates the convention)
    assert "name" not in fm, (
        f"{skill_file}: frontmatter must not include 'name' field "
        f"(slash command is derived from <plugin-name>:<dir-name>)"
    )

    # Body after frontmatter must be non-empty
    assert post.strip(), f"{skill_file}: body after frontmatter is empty"


def test_expert_roster_sql_uses_correct_column_alias():
    from pathlib import Path
    skill_path = Path(__file__).resolve().parents[1] / "internal" / "expert-roster" / "SKILL.md"
    content = skill_path.read_text()
    assert "r.role_name" not in content, \
        "SQL uses r.role_name which does not exist; should be r.name AS role_name"
    assert "r.name AS role_name" in content
