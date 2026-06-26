"""Tests for the tokenmeter scenario value type + loader (Cycle-2).

All fixtures are built INLINE under ``tmp_path`` (plus the one checked-in example
``benchmark/scenarios/improve.json``). No real ``claude`` is involved — a scenario is
a pure value object. Stdlib + pytest only; scenario JSON is DATA.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.tokenmeter_scenario import (
    DIFFERENTIATION_FILTER_CLAUSE,
    Scenario,
    auto_generate_scenario,
    compute_scenario_hash,
    from_mapping,
    load_scenario,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


# ── stable hash ──────────────────────────────────────────────────────────────


def test_scenario_hash_is_stable_over_prompt_and_target():
    """The hash is a deterministic 16-hex digest of prompt + target."""
    h1 = compute_scenario_hash("do the thing", "skills/improve")
    h2 = compute_scenario_hash("do the thing", "skills/improve")
    assert h1 == h2
    assert len(h1) == 16
    assert all(c in "0123456789abcdef" for c in h1)


def test_scenario_hash_changes_with_prompt_or_target():
    base = compute_scenario_hash("prompt A", "skills/improve")
    assert base != compute_scenario_hash("prompt B", "skills/improve")
    assert base != compute_scenario_hash("prompt A", "internal/cycle")


def test_scenario_hash_is_content_independent():
    """The hash covers the target PATH, never the target's file contents — so the
    improved version (same path, same prompt) keeps the hash stable. This is what
    makes a before/after delta legitimate under the control-vector gate."""
    s = Scenario.create(name="s", target="skills/improve", prompt="p")
    assert s.scenario_hash == compute_scenario_hash("p", "skills/improve")
    # No file was read to compute it.


def test_create_recomputes_hash_and_validates():
    s = Scenario.create(name="demo", target="skills/improve", prompt="exercise it")
    assert s.scenario_hash == compute_scenario_hash("exercise it", "skills/improve")
    assert s.source == "user"
    assert s.cycles == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "", "target": "t", "prompt": "p"},
        {"name": "n", "target": "", "prompt": "p"},
        {"name": "n", "target": "t", "prompt": ""},
        {"name": "n", "target": "t", "prompt": "p", "source": "bogus"},
    ],
)
def test_create_rejects_malformed(kwargs):
    with pytest.raises(ValueError):
        Scenario.create(**kwargs)


# ── loader ───────────────────────────────────────────────────────────────────


def test_load_scenario_from_file(tmp_path):
    path = tmp_path / "scn.json"
    path.write_text(
        json.dumps(
            {
                "name": "scn",
                "target": "internal/cycle",
                "prompt": "run a cycle",
                "source": "user",
                "cycles": 2,
                "subject": "leaner cycle",
            }
        ),
        encoding="utf-8",
    )
    s = load_scenario(path)
    assert s.name == "scn"
    assert s.target == "internal/cycle"
    assert s.cycles == 2
    assert s.subject == "leaner cycle"
    assert s.scenario_hash == compute_scenario_hash("run a cycle", "internal/cycle")


def test_loader_ignores_stored_hash(tmp_path):
    """A stale stored scenario_hash is IGNORED — the loader recomputes it so the
    hash can never drift from the fields it summarizes."""
    path = tmp_path / "scn.json"
    path.write_text(
        json.dumps(
            {
                "name": "scn",
                "target": "skills/improve",
                "prompt": "p",
                "scenario_hash": "deadbeefdeadbeef",
            }
        ),
        encoding="utf-8",
    )
    s = load_scenario(path)
    assert s.scenario_hash == compute_scenario_hash("p", "skills/improve")
    assert s.scenario_hash != "deadbeefdeadbeef"


def test_load_scenario_bad_json_raises(tmp_path):
    path = tmp_path / "scn.json"
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ValueError):
        load_scenario(path)


def test_from_mapping_rejects_non_object():
    with pytest.raises(ValueError):
        from_mapping(["not", "an", "object"])


def test_resolve_target_relative_and_absolute(tmp_path):
    s = Scenario.create(name="s", target="skills/improve", prompt="p")
    assert s.resolve_target(repo_root=tmp_path) == tmp_path / "skills" / "improve"
    abs_target = tmp_path / "abs"
    s_abs = Scenario.create(name="s", target=str(abs_target), prompt="p")
    assert s_abs.resolve_target(repo_root="/somewhere/else") == abs_target


# ── the checked-in example scenario ──────────────────────────────────────────


def test_example_scenario_file_is_valid_and_targets_a_real_skill():
    """The shipped example loads and points at a real skill directory."""
    s = load_scenario(REPO_ROOT / "benchmark" / "scenarios" / "improve.json")
    assert s.target == "skills/improve"
    assert s.prompt
    assert s.source == "user"
    assert (REPO_ROOT / s.target / "SKILL.md").is_file()
    assert s.scenario_hash == compute_scenario_hash(s.prompt, s.target)


# ── auto-generated scenarios (Cycle-3; heuristic-first, NO LLM) ───────────────


def _make_skill(
    root,
    *,
    description="Use this skill to summarize a target file in exactly three sentences.",
    with_usage=True,
):
    """Build an inline plugin tree → the skill dir under ``root`` (repo root)."""
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "demo"}), encoding="utf-8"
    )
    skill_dir = root / "skills" / "widget"
    skill_dir.mkdir(parents=True)
    body = "# widget\n\nDoes a representative thing.\n"
    if with_usage:
        body += "\n## Usage\n\n```\nwidget:run <path> --mode fast\n```\n"
    (skill_dir / "SKILL.md").write_text(
        f"---\ndescription: {description}\n---\n\n{body}", encoding="utf-8"
    )
    return skill_dir


class _FakeRunner:
    """Async injectable claude runner returning a fixed ``result`` envelope."""

    def __init__(self, text):
        self.text = text
        self.calls = []

    async def __call__(self, argv, cwd=None):
        self.calls.append(list(argv))
        return json.dumps({"result": self.text, "total_cost_usd": 0.01, "is_error": False})


class _BoomRunner:
    async def __call__(self, argv, cwd=None):
        raise RuntimeError("synthesis failed")


def test_auto_generate_heuristic_is_stable_and_valid(tmp_path):
    """Heuristic auto-gen (NO LLM) yields a stable Scenario(source='auto') with a
    non-empty prompt + a valid 16-hex hash, and is deterministic across calls."""
    skill_dir = _make_skill(tmp_path)

    s1 = auto_generate_scenario(skill_dir)
    s2 = auto_generate_scenario(skill_dir)

    assert s1.source == "auto"
    assert s1.target == "skills/widget"  # made repo-relative
    assert s1.prompt  # non-empty
    assert len(s1.scenario_hash) == 16
    assert all(c in "0123456789abcdef" for c in s1.scenario_hash)
    assert s1.scenario_hash == compute_scenario_hash(s1.prompt, s1.target)

    # Deterministic: same skill dir → identical prompt + hash (no wall clock / randomness).
    assert s1.prompt == s2.prompt
    assert s1.scenario_hash == s2.scenario_hash

    # The prompt mines the description + the documented example + the differentiation filter.
    assert "three sentences" in s1.prompt
    assert "widget:run" in s1.prompt
    assert DIFFERENTIATION_FILTER_CLAUSE in s1.prompt
    assert "do not modify any files" in s1.prompt


def test_auto_generate_works_with_no_skill_md(tmp_path):
    """A skill dir lacking SKILL.md still produces a non-empty, valid auto Scenario."""
    skill_dir = tmp_path / "skills" / "bare"
    skill_dir.mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text("[tool.x]\n", encoding="utf-8")

    s = auto_generate_scenario(skill_dir)
    assert s.source == "auto"
    assert s.prompt
    assert s.scenario_hash == compute_scenario_hash(s.prompt, s.target)
    assert DIFFERENTIATION_FILTER_CLAUSE in s.prompt


def test_user_and_auto_feed_the_same_set_of_interests(tmp_path):
    """Both sources produce the SAME-shaped Scenario the runner/schema consume — only
    the ``source`` label (and how the prompt is produced) differ."""
    skill_dir = _make_skill(tmp_path)

    auto = auto_generate_scenario(skill_dir)
    user = Scenario.create(name="hand", target=auto.target, prompt="hand-written prompt")

    assert auto.source == "auto"
    assert user.source == "user"
    # Identical dataclass shape (same fields) → both flow through load/run/schema the same.
    assert {f.name for f in auto.__dataclass_fields__.values()} == {
        f.name for f in user.__dataclass_fields__.values()
    }
    for s in (auto, user):
        assert s.prompt
        assert len(s.scenario_hash) == 16
        assert s.scenario_hash == compute_scenario_hash(s.prompt, s.target)


def test_auto_generate_optional_runner_enriches_prompt(tmp_path):
    """The OPTIONAL injectable runner synthesizes a richer prompt via ONE claude call,
    with the guardrail clauses appended; source stays 'auto' and the hash stays valid."""
    skill_dir = _make_skill(tmp_path)
    runner = _FakeRunner("Refactor the widget loader and add a focused regression test.")

    s = auto_generate_scenario(skill_dir, runner=runner)

    assert len(runner.calls) == 1  # exactly one synthesis call
    argv = runner.calls[0]
    assert argv[0] == "claude" and "--output-format" in argv  # headless json, injectable
    assert "Refactor the widget loader" in s.prompt  # the synthesized text is used
    assert DIFFERENTIATION_FILTER_CLAUSE in s.prompt  # guardrails still appended
    assert s.source == "auto"
    assert s.scenario_hash == compute_scenario_hash(s.prompt, s.target)


def test_auto_generate_runner_failure_falls_back_to_heuristic(tmp_path):
    """A failing runner NEVER breaks auto-gen — it falls back to the heuristic prompt."""
    skill_dir = _make_skill(tmp_path)

    heuristic = auto_generate_scenario(skill_dir)
    fallback = auto_generate_scenario(skill_dir, runner=_BoomRunner())

    assert fallback.source == "auto"
    assert fallback.prompt == heuristic.prompt  # identical to the NO-LLM path
