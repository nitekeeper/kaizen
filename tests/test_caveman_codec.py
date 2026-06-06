"""Tests for scripts/caveman_codec.py + the AI-3 team_executor wiring.

Coverage:
  - protected-token byte-fidelity (code / URLs / paths / identifiers /
    version numbers / error-strings survive verbatim),
  - each level (off / lite / full),
  - idempotence (compress(compress(x)) == compress(x)),
  - auto-clarity carve-outs (security / destructive / multi-step → not
    compressed),
  - the AI-3 sink (`compress_reply_for_context`) gate behavior + the
    parser-untouched proof (parsers receive RAW bytes even when ON).
"""

from __future__ import annotations

import pytest

from scripts.caveman_codec import (
    DEFAULT_LEVEL,
    LEVELS,
    compress,
    should_compress,
)

# ── levels ────────────────────────────────────────────────────────────────


def test_levels_constant_shape():
    assert LEVELS == ("off", "lite", "full")
    assert DEFAULT_LEVEL == "full"


def test_off_level_is_identity():
    text = "I will just basically fix the the bug, please."
    assert compress(text, "off") == text


def test_unknown_level_raises():
    with pytest.raises(ValueError):
        compress("hello", "ultra")


def test_lite_keeps_articles_and_sentences():
    text = "I will just fix the bug, perhaps, in the module."
    out = compress(text, "lite")
    # filler ("just") + hedging ("perhaps") dropped...
    assert "just" not in out
    assert "perhaps" not in out
    # ...but articles + leader survive (lite keeps full sentences).
    assert "the bug" in out
    assert "the module" in out
    assert out.startswith("I will")


def test_full_drops_articles_filler_pleasantries_leaders():
    # Leader ("I will") is stripped only when it leads the line, so place it
    # first; pleasantry / filler / articles drop regardless of position.
    text = "I will please just basically restart the service now."
    out = compress(text, "full")
    for dropped in ("Please", "I will", "just", "basically", "the "):
        assert dropped.lower() not in out.lower(), dropped
    assert "restart" in out.lower()
    assert "service" in out


def test_default_level_is_full():
    text = "I will just restart the service."
    assert compress(text) == compress(text, "full")


# ── protected-token byte-fidelity ─────────────────────────────────────────


@pytest.mark.parametrize(
    "segment",
    [
        "`<=`",  # inline code with operators
        "`auth.py`",  # inline code path
        "MAX_RETRIES",  # CONST_CASE
        "DB_CONN_TIMEOUT_S",  # CONST_CASE
        "foo.bar()",  # dotted call
        "scripts.team_executor",  # dotted identifier
        "v2.10.1",  # version-like
        "1.2.3",  # semver
        "/etc/app/config.yaml",  # absolute path
        "scripts/caveman_codec.py",  # relative path
        "https://example.com/a?b=c",  # URL
        "http://x.io",  # URL
        "git@github.com:owner/repo.git",  # git SCP URL
        "'connection refused'",  # quoted error string
        '"NoneType has no attribute"',  # quoted error string
    ],
)
def test_protected_segment_survives_byte_identical(segment):
    text = f"The really just simple thing is {segment} and please verify it."
    out = compress(text, "full")
    assert segment in out, f"{segment!r} not preserved in {out!r}"


def test_fenced_code_block_survives_byte_identical():
    code = "```python\nthe a an just really\nx = the_value\n```"
    text = f"Please look at this just code block:\n{code}\nand really fix it."
    out = compress(text, "full")
    assert code in out


def test_filler_inside_quoted_error_string_is_preserved():
    # 'just' inside a quoted error string must NOT be stripped.
    text = "We hit the error 'just kidding the value is null' really."
    out = compress(text, "full")
    assert "'just kidding the value is null'" in out


def test_path_with_article_prefix_not_fused():
    text = "Open the /etc/hosts file now."
    out = compress(text, "full")
    assert "/etc/hosts" in out
    # the path token is not glued to a neighboring word
    assert "the/etc/hosts" not in out


# ── idempotence ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("level", ["lite", "full"])
@pytest.mark.parametrize(
    "text",
    [
        "I will just basically fix the `auth.py` bug in the middleware.",
        "Please update the function foo.bar() and the version 1.2.3 string.",
        "Maybe we could potentially restart the the service.",
        "The CONST_VALUE and MY_FLAG identifiers must not change, really.",
        "Bullet review:\n- the first item is just fine\n- really the second is `code` here",
        "We need to call git@github.com:owner/repo.git and check 'connection refused'.",
    ],
)
def test_idempotent(level, text):
    once = compress(text, level)
    twice = compress(once, level)
    assert once == twice


def test_off_idempotent():
    text = "The a an just really thing."
    assert compress(compress(text, "off"), "off") == compress(text, "off")


# ── empty / non-string input ──────────────────────────────────────────────


def test_empty_string_identity():
    assert compress("", "full") == ""


def test_non_string_identity():
    assert compress(None, "full") is None  # type: ignore[arg-type]
    assert compress(123, "full") == 123  # type: ignore[arg-type]


# ── auto-clarity gate (should_compress) ───────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "Security warning: this exposes a credential.",
        "This is a known CVE-2021-1234 vulnerability.",
        "Caution: irreversible operation ahead.",
        "This change is destructive and cannot be undone.",
        "Run rm -rf /tmp/build to clean up.",
        "Please confirm: approve? before continuing.",
        "Reply confirm? to proceed.",
        "We must DROP TABLE users now.",
        "Then git push --force origin main.",
        "Just git push -f and move on.",
        "Recovery: git reset --hard HEAD~1.",
        "1. clone repo\n2. run setup\n3. open PR",
        "Steps:\n1) first\n2) second\n3) third\n4) fourth",
    ],
)
def test_should_compress_false_on_tripwires(text):
    assert should_compress(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "Refactored the auth middleware to reuse the pool.",
        "Found a minor naming inconsistency in the parser.",
        # M4 — bare destructive VERBS in ordinary prose stay compressible.
        "delete the unused import",
        "force-push protection looks fine",
        "We should delete the dead code path here.",
        "The truncate helper handles the edge case.",
        "Drop the redundant index on the join column.",
        "1. only one numbered line here is fine to compress",
        "Two steps:\n1. first\n2. second",  # only 2 steps → still compressible
    ],
)
def test_should_compress_true_on_safe_prose(text):
    assert should_compress(text) is True


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # M4 explicit cases from the review.
        ("delete the unused import", True),
        ("run rm -rf /tmp/x", False),
        ("DROP TABLE users", False),
    ],
)
def test_should_compress_m4_imperative_vs_bare_verb(text, expected):
    assert should_compress(text) is expected


@pytest.mark.parametrize(
    "text",
    [
        # N2 — destructive SQL/git IMPERATIVES now tripwire to False.
        "DELETE FROM users WHERE id = 1",
        "TRUNCATE TABLE sessions",
        "git clean -fd to wipe untracked files",
    ],
)
def test_should_compress_false_on_n2_imperatives(text):
    assert should_compress(text) is False


@pytest.mark.parametrize(
    "text",
    [
        # N2 — the BARE verbs in ordinary prose still compress (no regression).
        "delete the stale rows from the cache map",
        "truncate the overly long log line for display",
        "git clean handling looks fine in the helper",
    ],
)
def test_should_compress_true_on_n2_bare_verb_prose(text):
    assert should_compress(text) is True


def test_should_compress_false_on_empty_and_non_string():
    assert should_compress("") is False
    assert should_compress("   ") is False
    assert should_compress(None) is False  # type: ignore[arg-type]


# ── AI-3 sink: gate + parser-untouched proof ──────────────────────────────


def test_compress_reply_off_is_byte_identical(monkeypatch):
    """Gate OFF ⇒ compress_reply_for_context returns input unchanged."""
    from scripts.team_executor import compress_reply_for_context

    monkeypatch.setenv("KAIZEN_CAVEMAN_COMPRESS", "0")
    text = "I will just basically restart the service please."
    assert compress_reply_for_context(text) == text

    monkeypatch.setenv("KAIZEN_CAVEMAN_COMPRESS", "false")
    assert compress_reply_for_context(text) == text


def test_compress_reply_on_actually_compresses(monkeypatch):
    """Gate explicitly ON ⇒ filler is stripped from the model-bound prose."""
    from scripts.team_executor import compress_reply_for_context

    monkeypatch.setenv("KAIZEN_CAVEMAN_COMPRESS", "1")
    text = "I will just basically restart the service."
    out = compress_reply_for_context(text)
    assert out != text
    assert "just" not in out
    assert "basically" not in out
    assert "restart" in out.lower()


def test_compress_reply_default_is_off(monkeypatch):
    """M8 — unset env ⇒ default OFF (opt-in); input returned byte-identical."""
    from scripts.team_executor import compress_reply_for_context

    monkeypatch.delenv("KAIZEN_CAVEMAN_COMPRESS", raising=False)
    text = "I will just restart the service."
    assert compress_reply_for_context(text) == text


def test_compress_reply_unrecognised_value_is_off(monkeypatch):
    """M8 — only explicit truthy markers enable; a junk value stays OFF."""
    from scripts.team_executor import compress_reply_for_context

    monkeypatch.setenv("KAIZEN_CAVEMAN_COMPRESS", "maybe")
    text = "I will just restart the service."
    assert compress_reply_for_context(text) == text


def test_compress_reply_passes_through_tripwire(monkeypatch):
    """Gate ON but auto-clarity tripwire ⇒ verbatim pass-through."""
    from scripts.team_executor import compress_reply_for_context

    monkeypatch.setenv("KAIZEN_CAVEMAN_COMPRESS", "1")
    text = "Security warning: rm -rf the directory is irreversible."
    assert compress_reply_for_context(text) == text


def test_reviewer_parser_receives_uncompressed_input(monkeypatch):
    """AI-3 proof: `_parse_reviewer_response` extracts identical findings
    whether or not the caveman feature is ON — because the parser is fed RAW
    bytes upstream of any compression. We assert structured equality between
    the parse of the original reply and the parse of the original reply when
    the feature gate is ON (the gate must NOT sit between the wire and the
    parser).
    """
    from scripts.team_executor import _parse_reviewer_response

    # A reviewer reply laden with strippable filler/articles AND a real
    # finding line in the byte-sensitive grammar.
    reply = (
        "I will just basically review the change.\n"
        "[major] scripts/team_executor.py:42 - the really obvious off-by-one bug\n"
        "Please note this is the only issue."
    )

    monkeypatch.setenv("KAIZEN_CAVEMAN_COMPRESS", "1")  # feature ON
    findings = _parse_reviewer_response(reply, "reviewer-1", prefix="R1")

    assert len(findings) == 1
    f = findings[0]
    assert f.severity == "major"
    assert f.file_line == "scripts/team_executor.py:42"
    # The finding text is the RAW (uncompressed) text — filler/articles intact.
    assert f.finding == "the really obvious off-by-one bug"
    assert f.reviewer == "reviewer-1"
    assert f.finding_id == "R1-1"


def test_compressing_a_reviewer_reply_would_change_finding_text():
    """Sanity backstop for the proof above: prove that IF the compressed copy
    had been fed to the parser instead of the raw reply, the extracted finding
    text WOULD differ. This is what makes the parser-untouched proof
    meaningful (the two paths are genuinely distinguishable).
    """
    from scripts.team_executor import _parse_reviewer_response

    raw_line = "[major] scripts/team_executor.py:42 - the really obvious off-by-one bug"

    raw_findings = _parse_reviewer_response(raw_line, "r", prefix="R1")
    compressed = compress(raw_line, "full")
    compressed_findings = _parse_reviewer_response(compressed, "r", prefix="R1")

    # Both still parse (the finding grammar's protected tokens survive), but
    # the finding TEXT differs because compression strips "the"/"really" from
    # the free-text tail. This demonstrates that feeding the compressed copy
    # to the parser would corrupt the structured data — hence AI-3 must (and
    # does) feed the parser raw.
    assert raw_findings[0].finding == "the really obvious off-by-one bug"
    assert compressed_findings and compressed_findings[0].finding != raw_findings[0].finding


def test_agenda_and_action_item_parsers_unaffected_by_compression(monkeypatch):
    """The other byte-sensitive parsers (`_parse_agenda_items`,
    `_parse_action_items`) are likewise never fed compressed bytes. We assert
    they parse a raw response identically with the feature ON.
    """
    import json

    from scripts.team_executor import _parse_action_items, _parse_agenda_items

    monkeypatch.setenv("KAIZEN_CAVEMAN_COMPRESS", "1")

    agenda = "# header to skip\nthe really first item\njust the second item"
    items = _parse_agenda_items(agenda)
    # Raw lines preserved verbatim (filler/articles intact — not compressed).
    assert items == ["the really first item", "just the second item"]

    payload = [{"id": "A1", "touches": ["the_file.py"]}]
    close_resp = f"Here is the plan, please:\n```json\n{json.dumps(payload)}\n```\n"
    parsed = _parse_action_items(close_resp)
    assert parsed == payload


# ── M5 — sentinel collision must not crash ────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "before \x005\x00 after the just bug",  # literal sentinel-looking span
        "hexdump \x000\x00\x001\x00 just here",  # multiple
        "\x00999\x00 leading just sentinel",  # out-of-range index
        "trailing just sentinel \x0042\x00",
    ],
)
def test_sentinel_collision_does_not_crash(text):
    # Must not raise IndexError; output is well-defined.
    out = compress(text, "full")
    assert isinstance(out, str)
    # NUL never survives into the output (stripped before masking).
    assert "\x00" not in out


def test_sentinel_collision_preserves_protected_after_strip():
    # A real protected token alongside a sentinel-looking literal: the real
    # token survives, the literal NUL is stripped, no crash.
    text = "see `code` and \x007\x00 just now"
    out = compress(text, "full")
    assert "`code`" in out
    assert "\x00" not in out


# ── M7 — source case preserved when nothing stripped at the head ──────────


def test_m7_no_head_strip_preserves_leading_case():
    # "really" is mid-sentence filler; the head token "git" is NOT a stopword,
    # so it must stay lowercase (no spurious capitalization).
    out = compress("git commit really now", "full")
    assert out == "git commit now"


def test_m7_head_strip_recapitalizes():
    # Leading article dropped → new first word gets sentence-cased.
    assert compress("the table is just big", "full") == "Table is big"


def test_m7_lite_no_head_strip_preserves_case():
    out = compress("git status really matters", "lite")
    assert out.startswith("git ")
    assert "really" not in out


# ── B1 — _TERSE_OUTPUT_RULE wired into rendered prompts, before F7 ────────


@pytest.mark.parametrize(
    "render",
    [
        lambda: __import__(
            "scripts.dispatch_templates", fromlist=["phase_2_preanalysis"]
        ).phase_2_preanalysis(agenda_items=["item one"], participant="backend-engineer-1"),
        lambda: __import__(
            "scripts.dispatch_templates", fromlist=["phase_4_implementer"]
        ).phase_4_implementer(item={"id": "A1", "touches": ["x.py"], "reads": []}, wave_n=1),
        lambda: __import__(
            "scripts.dispatch_templates", fromlist=["phase_5b_prime_reviewer"]
        ).phase_5b_prime_reviewer(iter_n=1, action_items=[{"id": "A1"}]),
    ],
    ids=["phase_2", "phase_4", "phase_5b_reviewer"],
)
def test_b1_terse_rule_present_and_before_f7_trailer(render):
    from scripts.dispatch_templates import (
        _TERSE_OUTPUT_RULE,
        TEAMMATE_REPLY_RULE,
    )

    body = render()
    terse = _TERSE_OUTPUT_RULE.strip()
    trailer = TEAMMATE_REPLY_RULE.strip()

    # (a) terse rule appears in the rendered teammate prompt
    assert terse in body
    # (b) it appears BEFORE the F7 reply-rule trailer
    assert body.index(terse) < body.index(trailer)
    # F7 trailer remains the LAST instruction in the prompt
    assert body.rstrip().endswith(trailer)


def test_b1_terse_injection_does_not_mutate_trailer_bytes():
    """The B1 injection must leave the F7 trailer byte-identical (parity)."""
    from scripts.dispatch_templates import (
        TEAMMATE_REPLY_RULE,
        phase_4_implementer,
    )

    body = phase_4_implementer(item={"id": "A1", "touches": ["x.py"], "reads": []}, wave_n=1)
    trailer = TEAMMATE_REPLY_RULE.strip()
    # The trailer span in the rendered body is byte-for-byte the constant.
    assert body[body.index(trailer) :].rstrip() == trailer


# ── N3 — _inject_terse_before_trailer fails loud on a missing trailer ─────


def test_n3_inject_raises_on_missing_trailer():
    """A trailer-less body must raise ValueError (never silently append the
    terse rule after the F7 position)."""
    from scripts.dispatch_templates import _inject_terse_before_trailer

    with pytest.raises(ValueError, match="F7 reply-rule trailer"):
        _inject_terse_before_trailer("A teammate body with no trailer at all.")


def test_n3_inject_succeeds_when_trailer_present():
    """With the F7 trailer present, the helper injects cleanly (the happy path
    the 3 real callers rely on)."""
    from scripts.dispatch_templates import (
        _TERSE_OUTPUT_RULE,
        TEAMMATE_REPLY_RULE,
        _inject_terse_before_trailer,
    )

    trailer = TEAMMATE_REPLY_RULE.strip()
    body = "Phase body prose." + TEAMMATE_REPLY_RULE
    out = _inject_terse_before_trailer(body)
    assert _TERSE_OUTPUT_RULE.strip() in out
    assert out.index(_TERSE_OUTPUT_RULE.strip()) < out.index(trailer)
    assert out.rstrip().endswith(trailer)


def test_n3_real_callers_still_render_after_fail_loud_change():
    """N3 must not break the 3 real teammate-bound callers — each still renders
    with the terse rule before the (intact) F7 trailer."""
    from scripts.dispatch_templates import (
        _TERSE_OUTPUT_RULE,
        TEAMMATE_REPLY_RULE,
        phase_2_preanalysis,
        phase_4_implementer,
        phase_5b_prime_reviewer,
    )

    bodies = [
        phase_2_preanalysis(agenda_items=["item one"], participant="backend-engineer-1"),
        phase_4_implementer(item={"id": "A1", "touches": ["x.py"], "reads": []}, wave_n=1),
        phase_5b_prime_reviewer(iter_n=1, action_items=[{"id": "A1"}]),
    ]
    terse = _TERSE_OUTPUT_RULE.strip()
    trailer = TEAMMATE_REPLY_RULE.strip()
    for body in bodies:
        assert terse in body
        assert body.index(terse) < body.index(trailer)
        assert body.rstrip().endswith(trailer)


# ── B3 — token/char-delta proof at the B2 sink + codec ────────────────────


def test_b3_filler_heavy_input_comes_out_strictly_shorter():
    text = (
        "I will just basically restart the service, and really the cache "
        "should perhaps be cleared too, please."
    )
    out = compress(text, "full")
    assert len(out) < len(text), (len(text), len(out), out)


def test_b3_protected_only_input_is_byte_identical():
    # A reply made entirely of protected tokens (no strippable prose) must
    # come out byte-identical — compression saves nothing but loses nothing.
    text = "`code` MAX_RETRIES foo.bar() v1.2.3 /etc/app.conf https://x.io/a"
    assert compress(text, "full") == text


def test_b3_sink_char_delta_when_on(monkeypatch):
    """At the B2 sink (model-bound), gate ON shrinks filler-heavy prose and
    leaves protected-only prose byte-identical."""
    from scripts.team_executor import compress_reply_for_context

    monkeypatch.setenv("KAIZEN_CAVEMAN_COMPRESS", "1")

    filler = "I will just basically refactor the parser and really tidy the imports."
    shrunk = compress_reply_for_context(filler)
    assert len(shrunk) < len(filler)

    protected = "`x` MAX_RETRIES foo.bar() v1.2.3 /etc/app.conf"
    assert compress_reply_for_context(protected) == protected


# ── B2 — the model-bound sink (Phase-3 Star-open) leaves stored raw exact ──


def test_b2_phase3_open_compresses_broadcast_but_not_stored_raw(monkeypatch):
    """B2 proof: the Phase-3 Star-open broadcast prose is compressed, while the
    stored proposal `raw` (the parser/DB-bound copy) stays byte-exact.

    We exercise the exact list-comprehension the executor uses to build the
    broadcast copy, then confirm (a) the broadcast prose is shorter, and
    (b) the source `proposals` list is unchanged.
    """
    from scripts.team_executor import compress_reply_for_context

    monkeypatch.setenv("KAIZEN_CAVEMAN_COMPRESS", "1")

    raw_text = "I will just basically propose that we really refactor the auth layer."
    proposals = [{"agent": "backend-engineer-1", "raw": raw_text}]

    # Mirror the executor's B2 sink construction.
    broadcast = [
        {"agent": p["agent"], "raw": compress_reply_for_context(p["raw"])} for p in proposals
    ]

    # Stored copy untouched (parser/DB-bound).
    assert proposals[0]["raw"] == raw_text
    # Broadcast copy compressed (model-bound).
    assert broadcast[0]["raw"] != raw_text
    assert len(broadcast[0]["raw"]) < len(raw_text)


def test_b2_phase3_open_broadcast_off_is_byte_identical(monkeypatch):
    """Gate OFF (default) ⇒ the broadcast copy equals the stored raw."""
    from scripts.team_executor import compress_reply_for_context

    monkeypatch.delenv("KAIZEN_CAVEMAN_COMPRESS", raising=False)

    raw_text = "I will just basically propose that we really refactor the auth layer."
    assert compress_reply_for_context(raw_text) == raw_text


# ── N1 — END-TO-END proof that drives the REAL executor (anti-revert) ─────
#
# The two `test_b2_phase3_open_*` tests above mirror the executor's
# list-comprehension in TEST CODE — they prove the helper composes correctly
# but would stay GREEN if the production callsite
# (`team_executor.py` Phase-3 broadcast) were reverted to
# `phase_3_open(proposals=proposals)`. THIS test closes that anti-revert hole
# by driving `team_cycle_executor` end-to-end and inspecting the ACTUAL
# Phase-3 Star-open message body the executor broadcasts. Neuter-check
# (recorded in the PR/review notes): reverting the Phase-3 callsite to
# broadcast the uncompressed copy makes this test go RED.

# Filler-heavy Phase-2 proposal: compresses to a visibly different, strictly
# shorter prose so the broadcast body is distinguishable from the raw.
_N1_RAW_PROPOSAL = (
    "I will just basically propose that we really refactor the auth layer now and tidy imports."
)


def _n1_run_executor_capturing_phase3_open(monkeypatch, tmp_path, gate_value):
    """Drive `team_cycle_executor` through Phase 3 with the caveman gate set to
    ``gate_value`` and return ``(phase3_open_bodies, stored_proposal_raws)``.

    A capturing mock records the FULL (untruncated) Phase-3 Star-open message
    bodies. We also stub Phase 5c commit + git rev-parse and force CI green so
    the cycle reaches a clean success without a real repo.
    """
    from unittest.mock import patch

    from scripts import team_executor as te

    if gate_value is None:
        monkeypatch.delenv("KAIZEN_CAVEMAN_COMPRESS", raising=False)
    else:
        monkeypatch.setenv("KAIZEN_CAVEMAN_COMPRESS", gate_value)

    # Stub Phase 5c commit + rev-parse (no real repo) and CI (always green).
    monkeypatch.setattr(te, "commit_cycle", lambda **kwargs: None)

    class _FakeProc:
        stdout = "deadbeefcafebabe1234567890abcdef12345678\n"
        returncode = 0

    monkeypatch.setattr(te.subprocess, "run", lambda cmd, **kwargs: _FakeProc())
    monkeypatch.setattr(
        te,
        "run_ci_checks",
        lambda clone_dir, test_command: (True, {"tests": {"status": "pass", "output": "ok"}}),
    )

    phase3_open_bodies: list[str] = []

    class _CaptureTools:
        def __init__(self):
            self.calls: list = []

        def team_create(self, name, members):
            return f"team-{name}"

        def send_message(self, team_id, to, message):
            if "Phase 3 open" in message:
                phase3_open_bodies.append(message)
            # Scripted, phase-keyed replies that drive a clean success path.
            if "Phase 1" in message:
                return "do the work"
            if "Phase 2" in message:
                return _N1_RAW_PROPOSAL
            if "Phase 3 close" in message:
                return (
                    'ok\n```json\n[{"id": "A", "touches": ["x.py"], "reads": [], '
                    '"depends_on": [], "wave": 1, "owner": "backend-engineer-1"}]\n```'
                )
            if "Phase 4 wave" in message:
                return "OK: applied the change. tests: pass"
            if "Phase 5b'" in message:
                return "NO ISSUES"
            return "ack"

        def send_message_many(self, messages, *, quorum_floor=None):
            return [self.send_message(m["team_id"], m["to"], m["message"]) for m in messages]

        def team_delete(self, team_id):
            pass

        def apply_layout(self, team_id):
            pass

    tools = _CaptureTools()
    roster = ["pm-1", "backend-engineer-1"]
    project = {
        "name": "test-project",
        "git_url": "https://example.invalid/test.git",
        "expert_roster": roster,
        "test_command": "pytest",
    }
    with patch.dict(__import__("os").environ, {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}):
        outcome = te.team_cycle_executor(
            clone_dir=tmp_path,
            project=project,
            run_row={"id": 1, "subject": "test subject"},
            cycle_n=1,
            tools=tools,
        )
    return outcome, phase3_open_bodies


def test_n1_real_executor_phase3_broadcast_is_compressed_when_on(monkeypatch, tmp_path):
    """Drives the REAL `team_cycle_executor`: with the gate ON, the actual
    Phase-3 Star-open broadcast body carries the COMPRESSED proposal prose,
    NOT the raw filler-heavy form. This is the anti-revert proof — reverting
    the production callsite to broadcast `proposals` (raw) turns this RED.
    """
    outcome, bodies = _n1_run_executor_capturing_phase3_open(monkeypatch, tmp_path, "1")

    assert outcome["status"] == "success", f"unexpected outcome: {outcome}"
    assert bodies, "executor never broadcast a Phase-3 open message"

    compressed = compress(_N1_RAW_PROPOSAL, "full")
    assert compressed != _N1_RAW_PROPOSAL  # sanity: filler actually strips

    for body in bodies:
        # The model-bound broadcast carries the COMPRESSED prose...
        assert compressed in body, (
            "Phase-3 open broadcast did not contain the compressed proposal "
            f"prose {compressed!r}; body excerpt: {body[:400]!r}"
        )
        # ...and NOT the raw filler-heavy form (anti-revert tell).
        assert _N1_RAW_PROPOSAL not in body, (
            "Phase-3 open broadcast leaked the RAW (uncompressed) proposal — "
            "the production sink was bypassed/reverted."
        )


def test_n1_real_executor_phase3_broadcast_is_raw_when_off(monkeypatch, tmp_path):
    """Same real-executor drive with the gate OFF (default): the Phase-3
    broadcast body is byte-identical to the raw proposal (OFF restores prior
    behavior end-to-end)."""
    outcome, bodies = _n1_run_executor_capturing_phase3_open(monkeypatch, tmp_path, None)

    assert outcome["status"] == "success", f"unexpected outcome: {outcome}"
    assert bodies, "executor never broadcast a Phase-3 open message"
    for body in bodies:
        assert _N1_RAW_PROPOSAL in body, (
            f"Gate OFF must broadcast the RAW proposal verbatim; body excerpt: {body[:400]!r}"
        )
