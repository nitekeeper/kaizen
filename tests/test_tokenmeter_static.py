"""Tests for scripts/tokenmeter_static.py — deterministic static footprint.

Fixtures are built inline under ``tmp_path`` (no network, no live model). The
optional measured HTTP path is exercised only via injected fake counters — these
tests never touch the Anthropic API.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from scripts.tokenmeter_static import (
    APPROX_DIVISOR,
    MODE,
    NA,
    SOURCE_APPROX,
    SOURCE_MEASURED,
    TIER_ACTIVE_CORE,
    TIER_ACTIVE_REFERENCED,
    TIER_PASSIVE,
    TIERS,
    AnthropicTokenCounter,
    ApproxTokenCounter,
    approximate_tokens,
    default_tokenizer,
    enumerate_footprint,
    extract_description,
    scan_referenced_paths,
    split_frontmatter,
    static_footprint,
)

# ── Fixture builder ──────────────────────────────────────────────────────────

_DESCRIPTION = "Use when the user wants to run improvement cycles against a repo."
_SKILL_BODY = (
    "# improve\n\n"
    "Routes through `internal/run/SKILL.md` which does orchestration.\n"
    "Deterministic infra lives in `scripts/db.py` and `scripts/clone.py`.\n"
    "The dispatch uses `templates/cycle.md` for rendering.\n"
    "A missing reference `scripts/does_not_exist.py` is scanned but not counted.\n"
)


def _make_plugin(root: Path) -> Path:
    """Build a minimal plugin tree and return the skill dir."""
    (root / ".claude-plugin").mkdir()
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "demo", "version": "0.1.0"}), encoding="utf-8"
    )
    (root / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": "demo", "plugins": []}), encoding="utf-8"
    )
    (root / "scripts").mkdir()
    (root / "scripts" / "db.py").write_text("# db module\nDB = 1\n", encoding="utf-8")
    (root / "scripts" / "clone.py").write_text("# clone module\nCLONE = 2\n", encoding="utf-8")
    (root / "internal" / "run").mkdir(parents=True)
    (root / "internal" / "run" / "SKILL.md").write_text(
        "---\ndescription: internal run\n---\n\nrun body\n", encoding="utf-8"
    )
    (root / "templates").mkdir()
    (root / "templates" / "cycle.md").write_text("cycle template\n", encoding="utf-8")

    skill_dir = root / "skills" / "improve"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\ndescription: {_DESCRIPTION}\n---\n\n{_SKILL_BODY}", encoding="utf-8"
    )
    return skill_dir


# ── Constants / approximation ────────────────────────────────────────────────


def test_approx_divisor_is_named_constant():
    assert APPROX_DIVISOR == 4.0


def test_na_marker_is_not_zero():
    assert NA != 0
    assert NA == "n/a"


def test_approximate_tokens_uses_divisor():
    text = "x" * 17
    assert approximate_tokens(text) == math.ceil(17 / APPROX_DIVISOR)


def test_approximate_tokens_empty_is_zero():
    assert approximate_tokens("") == 0


def test_approx_counter_source_is_approximated():
    counter = ApproxTokenCounter()
    assert counter.source == SOURCE_APPROX
    assert counter.count("abcd") == 1


# ── Frontmatter / description parsing ────────────────────────────────────────


def test_split_frontmatter_basic():
    fm, body = split_frontmatter("---\ndescription: hi\n---\n\nbody here\n")
    assert "description: hi" in fm
    assert body.strip() == "body here"


def test_split_frontmatter_none():
    fm, body = split_frontmatter("# no frontmatter\nbody\n")
    assert fm == ""
    assert body == "# no frontmatter\nbody\n"


def test_extract_description_inline():
    assert extract_description("description: hello world") == "hello world"


def test_extract_description_quoted():
    assert extract_description('description: "quoted value"') == "quoted value"


def test_extract_description_block_scalar():
    fm = "name: x\ndescription: >\n  line one\n  line two\nother: y\n"
    assert extract_description(fm) == "line one line two"


def test_extract_description_absent():
    assert extract_description("name: x\n") == ""


# ── Static reference scan (never executes) ───────────────────────────────────


def test_scan_referenced_paths_finds_all_patterns():
    found = scan_referenced_paths(_SKILL_BODY)
    assert "scripts/db.py" in found
    assert "scripts/clone.py" in found
    assert "internal/run/SKILL.md" in found
    assert "templates/cycle.md" in found


def test_scan_referenced_paths_sorted_and_deduped():
    body = "scripts/db.py scripts/db.py scripts/clone.py"
    found = scan_referenced_paths(body)
    assert found == sorted(set(found))
    assert found.count("scripts/db.py") == 1


# ── enumerate_footprint ──────────────────────────────────────────────────────


def test_enumerate_has_three_tiers(tmp_path):
    skill_dir = _make_plugin(tmp_path)
    records = enumerate_footprint(skill_dir)
    tiers_present = {r["tier"] for r in records}
    assert TIER_PASSIVE in tiers_present
    assert TIER_ACTIVE_CORE in tiers_present
    assert TIER_ACTIVE_REFERENCED in tiers_present


def test_enumerate_passive_is_description(tmp_path):
    skill_dir = _make_plugin(tmp_path)
    records = enumerate_footprint(skill_dir)
    passive = [r for r in records if r["tier"] == TIER_PASSIVE]
    assert len(passive) == 1
    assert passive[0]["text"] == _DESCRIPTION
    assert passive[0]["path"].endswith("::frontmatter.description")


def test_enumerate_active_core_is_body(tmp_path):
    skill_dir = _make_plugin(tmp_path)
    records = enumerate_footprint(skill_dir)
    core = [r for r in records if r["tier"] == TIER_ACTIVE_CORE]
    assert len(core) == 1
    assert "improve" in core[0]["text"]


def test_enumerate_referenced_only_existing_files(tmp_path):
    skill_dir = _make_plugin(tmp_path)
    records = enumerate_footprint(skill_dir)
    referenced = {r["path"] for r in records if r["tier"] == TIER_ACTIVE_REFERENCED}
    assert "scripts/db.py" in referenced
    assert "scripts/clone.py" in referenced
    assert "internal/run/SKILL.md" in referenced
    assert "templates/cycle.md" in referenced
    # Missing reference is scanned but NOT included (conditional load).
    assert "scripts/does_not_exist.py" not in referenced


def test_enumerate_includes_plugin_metadata(tmp_path):
    skill_dir = _make_plugin(tmp_path)
    records = enumerate_footprint(skill_dir)
    referenced = {r["path"] for r in records if r["tier"] == TIER_ACTIVE_REFERENCED}
    assert ".claude-plugin/plugin.json" in referenced
    assert ".claude-plugin/marketplace.json" in referenced


def test_enumerate_missing_skill_dir_is_empty(tmp_path):
    # No SKILL.md and no metadata → empty enumeration, no raise.
    assert enumerate_footprint(tmp_path / "nope") == []


# ── static_footprint structure ───────────────────────────────────────────────


def test_static_footprint_mode_is_static(tmp_path):
    skill_dir = _make_plugin(tmp_path)
    result = static_footprint(skill_dir)
    assert result["mode"] == MODE


def test_static_footprint_default_source_is_approximated(tmp_path):
    skill_dir = _make_plugin(tmp_path)
    result = static_footprint(skill_dir)
    assert result["files"]
    for entry in result["files"]:
        assert entry["source"] == SOURCE_APPROX


def test_static_footprint_input_side_only(tmp_path):
    skill_dir = _make_plugin(tmp_path)
    result = static_footprint(skill_dir)
    for entry in result["files"]:
        assert entry["input"] == entry["token_count"]
        # Non-applicable columns are the NA marker, NEVER 0.
        assert entry["output"] == NA
        assert entry["cache_read"] == NA
        assert entry["cache_write"] == NA
        assert entry["output"] != 0


def test_static_footprint_char_and_token_counts(tmp_path):
    skill_dir = _make_plugin(tmp_path)
    result = static_footprint(skill_dir)
    for entry in result["files"]:
        assert entry["char_count"] >= 0
        assert entry["token_count"] == math.ceil(entry["char_count"] / APPROX_DIVISOR)


def test_static_footprint_tier_totals_sum(tmp_path):
    skill_dir = _make_plugin(tmp_path)
    result = static_footprint(skill_dir)
    summed_tokens = sum(t["token_count"] for t in result["tiers"].values())
    summed_files = sum(t["file_count"] for t in result["tiers"].values())
    assert summed_tokens == result["totals"]["token_count"]
    assert summed_files == result["totals"]["file_count"]
    assert set(result["tiers"]) >= set(TIERS)


# ── Tokenizer injection / fallback ───────────────────────────────────────────


class _FakeMeasured:
    source = SOURCE_MEASURED

    def count(self, text: str) -> int:
        return len(text)  # 1 token per char — clearly distinct from approx


class _FakeFailing:
    source = SOURCE_MEASURED

    def count(self, text: str) -> int | None:
        return None  # always fails → caller must fall back


class _FakeRaising:
    source = SOURCE_MEASURED

    def count(self, text: str) -> int:
        raise RuntimeError("boom")  # must be swallowed, never propagated


def test_injected_measured_tokenizer_marks_source(tmp_path):
    skill_dir = _make_plugin(tmp_path)
    result = static_footprint(skill_dir, tokenizer=_FakeMeasured())
    for entry in result["files"]:
        assert entry["source"] == SOURCE_MEASURED
        assert entry["token_count"] == entry["char_count"]


def test_failing_tokenizer_falls_back_to_approx(tmp_path):
    skill_dir = _make_plugin(tmp_path)
    result = static_footprint(skill_dir, tokenizer=_FakeFailing())
    for entry in result["files"]:
        assert entry["source"] == SOURCE_APPROX
        assert entry["token_count"] == math.ceil(entry["char_count"] / APPROX_DIVISOR)


def test_raising_tokenizer_is_swallowed(tmp_path):
    skill_dir = _make_plugin(tmp_path)
    # Must not raise — degrades to approximation.
    result = static_footprint(skill_dir, tokenizer=_FakeRaising())
    for entry in result["files"]:
        assert entry["source"] == SOURCE_APPROX


# ── Anthropic counter (no network) ───────────────────────────────────────────


def test_anthropic_counter_no_key_returns_none(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    counter = AnthropicTokenCounter()
    assert counter.available() is False
    assert counter.count("anything") is None


def test_anthropic_counter_source_is_measured():
    assert AnthropicTokenCounter.source == SOURCE_MEASURED


def test_default_tokenizer_without_key_is_approx(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert isinstance(default_tokenizer(), ApproxTokenCounter)


def test_default_tokenizer_with_key_is_anthropic(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    assert isinstance(default_tokenizer(), AnthropicTokenCounter)
