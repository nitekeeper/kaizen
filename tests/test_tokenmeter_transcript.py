"""Tests for Seam B — transcript GROUND TRUTH (scripts.tokenmeter_transcript).

ALL fixtures are built inline with ``tmp_path`` / synthetic JSONL strings — there
are NO checked-in fixture files or directories. Filesystem-touching tests use
``tmp_path``; the pure parsing/aggregation tests inject line lists directly.
"""

from __future__ import annotations

import json

from scripts.tokenmeter_transcript import (
    aggregate_usage,
    discover_transcripts,
    parse_transcript_file,
    parse_transcript_line,
    read_agent_label,
    sum_token_usage,
)


def _assistant_line(
    *,
    message_id=None,
    request_id=None,
    usage=None,
    session_id="parent-sess",
    is_sidechain=False,
    has_usage_key=True,
) -> str:
    message: dict = {}
    if message_id is not None:
        message["id"] = message_id
    if has_usage_key:
        message["usage"] = usage if usage is not None else {}
    obj: dict = {"type": "assistant", "message": message, "sessionId": session_id}
    if request_id is not None:
        obj["requestId"] = request_id
    if is_sidechain:
        obj["isSidechain"] = True
    return json.dumps(obj)


# ── discovery (filesystem) ──────────────────────────────────────────────────


def test_discover_recurses_into_nested_subagents(tmp_path):
    projects = tmp_path / "projects" / "encoded-proj" / "sess-1"
    projects.mkdir(parents=True)
    (projects / "sess-1.jsonl").write_text("{}\n")
    subagents = projects / "subagents"
    subagents.mkdir()
    (subagents / "agent-abc.jsonl").write_text("{}\n")

    transcripts = tmp_path / "transcripts" / "deep" / "deeper"
    transcripts.mkdir(parents=True)
    (transcripts / "t.jsonl").write_text("{}\n")

    # A non-jsonl sibling must be ignored.
    (subagents / "agent-abc.meta.json").write_text("{}\n")

    found = discover_transcripts(config_dir=tmp_path)
    names = {p.name for p in found}
    assert names == {"sess-1.jsonl", "agent-abc.jsonl", "t.jsonl"}


def test_discover_missing_trees_is_empty(tmp_path):
    assert discover_transcripts(config_dir=tmp_path) == []


def test_discover_uses_env_when_no_arg(tmp_path, monkeypatch):
    proj = tmp_path / "projects"
    proj.mkdir()
    (proj / "x.jsonl").write_text("{}\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    found = discover_transcripts()
    assert [p.name for p in found] == ["x.jsonl"]


# ── per-line parsing ────────────────────────────────────────────────────────


def test_only_assistant_with_usage_key_counts():
    # Non-assistant → None.
    user = json.dumps({"type": "user", "message": {"usage": {"input_tokens": 9}}})
    assert parse_transcript_line(json.loads(user), "f.jsonl") is None
    # assistant but NO usage key → None.
    no_usage = _assistant_line(has_usage_key=False)
    assert parse_transcript_line(json.loads(no_usage), "f.jsonl") is None


def test_empty_usage_is_zero_token_row():
    line = _assistant_line(message_id="m1", request_id="r1", usage={})
    rec = parse_transcript_line(json.loads(line), "f.jsonl")
    assert rec is not None
    assert rec.usage.input_tokens == 0
    assert rec.usage.output_tokens == 0
    assert rec.kept_but_suspect is False


def test_top_level_usage_only_not_iterations():
    usage = {
        "input_tokens": 10,
        "output_tokens": 20,
        "cache_creation_input_tokens": 3,
        "cache_read_input_tokens": 4,
        # Double-count trap: this nested array must be IGNORED.
        "iterations": [
            {"input_tokens": 1000, "output_tokens": 2000},
            {"input_tokens": 1000, "output_tokens": 2000},
        ],
    }
    rec = parse_transcript_line(
        json.loads(_assistant_line(message_id="m", request_id="r", usage=usage)),
        "f.jsonl",
    )
    assert rec is not None
    assert rec.usage.input_tokens == 10
    assert rec.usage.output_tokens == 20
    assert rec.usage.cache_creation_input_tokens == 3
    assert rec.usage.cache_read_input_tokens == 4


def test_numeric_hardening():
    usage = {
        "input_tokens": True,  # bool rejected → 0
        "output_tokens": "55",  # str rejected → 0
        "cache_creation_input_tokens": -7,  # negative clamped → 0
        "cache_read_input_tokens": None,  # null → 0
    }
    rec = parse_transcript_line(
        json.loads(_assistant_line(message_id="m", request_id="r", usage=usage)),
        "f.jsonl",
    )
    assert rec is not None
    assert rec.usage.input_tokens == 0
    assert rec.usage.output_tokens == 0
    assert rec.usage.cache_creation_input_tokens == 0
    assert rec.usage.cache_read_input_tokens == 0
    assert rec.kept_but_suspect is False


def test_suspect_large_value_kept_but_flagged():
    usage = {"input_tokens": 10_000_001}
    rec = parse_transcript_line(
        json.loads(_assistant_line(message_id="m", request_id="r", usage=usage)),
        "f.jsonl",
    )
    assert rec is not None
    assert rec.usage.input_tokens == 10_000_001  # kept
    assert rec.kept_but_suspect is True  # but flagged


def test_dedup_key_variants():
    both = parse_transcript_line(
        json.loads(_assistant_line(message_id="m1", request_id="r1", usage={})),
        "f.jsonl",
    )
    assert both.dedup_key == "m1:r1"
    only_id = parse_transcript_line(
        json.loads(_assistant_line(message_id="m1", usage={})),
        "f.jsonl",
    )
    assert only_id.dedup_key == "message:m1"
    no_id = parse_transcript_line(
        json.loads(_assistant_line(usage={})),
        "f.jsonl",
    )
    assert no_id.dedup_key is None


# ── within-file dedup (per-field MAX) ────────────────────────────────────────


def test_within_file_merge_by_field_max():
    lines = [
        _assistant_line(
            message_id="m", request_id="r", usage={"input_tokens": 5, "output_tokens": 50}
        ),
        # Out-of-order streaming partial: bigger input, smaller output.
        _assistant_line(
            message_id="m", request_id="r", usage={"input_tokens": 8, "output_tokens": 30}
        ),
    ]
    recs = parse_transcript_file(lines, "f.jsonl")
    assert len(recs) == 1
    assert recs[0].usage.input_tokens == 8  # max
    assert recs[0].usage.output_tokens == 50  # max


def test_malformed_line_skipped_walk_continues():
    lines = [
        "not json at all {",
        "",
        _assistant_line(message_id="m", request_id="r", usage={"input_tokens": 7}),
    ]
    recs = parse_transcript_file(lines, "f.jsonl")
    assert len(recs) == 1
    assert recs[0].usage.input_tokens == 7


def test_unkeyed_lines_never_deduped():
    lines = [
        _assistant_line(usage={"input_tokens": 1}),
        _assistant_line(usage={"input_tokens": 1}),
    ]
    recs = parse_transcript_file(lines, "f.jsonl")
    assert len(recs) == 2
    assert all(r.dedup_key is None for r in recs)


# ── across-file dedup (mtime asc, first-wins) ────────────────────────────────


def test_across_file_first_wins_by_mtime():
    original = {
        "source": "original.jsonl",
        "mtime": 100.0,
        "lines": [_assistant_line(message_id="m", request_id="r", usage={"input_tokens": 11})],
    }
    resumed_copy = {
        "source": "resumed.jsonl",
        "mtime": 200.0,  # later → its duplicate must be dropped
        "lines": [_assistant_line(message_id="m", request_id="r", usage={"input_tokens": 999})],
    }
    # Pass later-first to prove ordering is by mtime, not input order.
    recs = aggregate_usage([resumed_copy, original])
    assert len(recs) == 1
    assert recs[0].usage.input_tokens == 11  # the ORIGINAL (oldest) wins
    assert recs[0].source == "original.jsonl"


def test_across_file_unkeyed_all_kept():
    files = [
        {"source": "a.jsonl", "mtime": 1.0, "lines": [_assistant_line(usage={"input_tokens": 1})]},
        {"source": "b.jsonl", "mtime": 2.0, "lines": [_assistant_line(usage={"input_tokens": 1})]},
    ]
    recs = aggregate_usage(files)
    assert len(recs) == 2


# ── sidechain inclusion ──────────────────────────────────────────────────────


def test_sidechain_counts_with_parent_session_and_label(tmp_path):
    # Real layout: <parent-session-uuid>/subagents/agent-xyz.jsonl + agent-xyz.meta.json.
    # The sidechain line's OWN sessionId field IS the parent session (verified across
    # real on-disk transcripts). NON-CIRCULAR: the dir is named differently from the
    # session id, so the assertion proves we read the sessionId field, not the dir.
    parent = tmp_path / "dir-name-differs-from-session-id"
    subagents = parent / "subagents"
    subagents.mkdir(parents=True)
    jsonl = subagents / "agent-xyz.jsonl"
    (subagents / "agent-xyz.meta.json").write_text(json.dumps({"agentType": "Explore"}))

    line = _assistant_line(
        message_id="sm",
        request_id="sr",
        usage={"input_tokens": 42},
        session_id="real-parent-session",  # the line's OWN sessionId IS the parent
        is_sidechain=True,
    )
    rec = parse_transcript_line(json.loads(line), jsonl, meta_lookup=read_agent_label)
    assert rec is not None
    assert rec.is_sidechain is True
    assert rec.usage.input_tokens == 42  # sidechain tokens still count
    assert rec.session_id == "real-parent-session"  # from the sessionId field, not the dir
    assert rec.session_id != "dir-name-differs-from-session-id"
    assert rec.agent_label == "Explore"


def test_sidechain_session_falls_back_to_path_when_no_session_id():
    # Defensive fallback: a sidechain line with NO sessionId recovers the parent from
    # the on-disk layout (.../<parent-session-uuid>/subagents/agent-*.jsonl).
    obj = {
        "type": "assistant",
        "isSidechain": True,
        "message": {"id": "sm", "usage": {"input_tokens": 1}},
        # NOTE: deliberately no "sessionId" key.
    }
    rec = parse_transcript_line(obj, "/x/recovered-parent-uuid/subagents/agent-xyz.jsonl")
    assert rec.session_id == "recovered-parent-uuid"  # parent.parent.name fallback


def test_sidechain_pure_without_meta_lookup():
    # No injected resolver → no filesystem access → label stays None (purity). The
    # session_id comes from the line's own sessionId field (the parent).
    line = _assistant_line(
        message_id="sm",
        request_id="sr",
        usage={"input_tokens": 1},
        session_id="parent-sess",
        is_sidechain=True,
    )
    rec = parse_transcript_line(json.loads(line), "/x/some-dir/subagents/agent-xyz.jsonl")
    assert rec.session_id == "parent-sess"  # the line's own sessionId, no fs access
    assert rec.agent_label is None


def test_read_agent_label_missing_meta_is_none(tmp_path):
    jsonl = tmp_path / "agent-none.jsonl"
    assert read_agent_label(jsonl) is None


# ── summation helper ─────────────────────────────────────────────────────────


def test_sum_token_usage():
    files = [
        {
            "source": "a.jsonl",
            "mtime": 1.0,
            "lines": [
                _assistant_line(
                    message_id="m1", request_id="r1", usage={"input_tokens": 5, "output_tokens": 1}
                ),
                _assistant_line(
                    message_id="m2", request_id="r2", usage={"input_tokens": 3, "output_tokens": 2}
                ),
            ],
        }
    ]
    total = sum_token_usage(aggregate_usage(files))
    assert total.input_tokens == 8
    assert total.output_tokens == 3
