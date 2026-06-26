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
    Scenario,
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
