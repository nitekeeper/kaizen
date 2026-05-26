"""Byte-parity guard: `_trailer.md` body == `TEAMMATE_REPLY_RULE`.

The F7 reply-contract + GAP-7 shutdown handshake live in TWO places
today:
  - `internal/cycle/templates/_trailer.md` — the on-disk partial that
    every Phase 1-7 .md template includes via the
    `{{ include: _trailer.md }}` directive (the substrate AI-3's loader
    rewire will read from).
  - `scripts.dispatch_templates.TEAMMATE_REPLY_RULE` — the Python
    constant that the live dispatch path concatenates to each rendered
    prompt today.

Until AI-3 makes `_trailer.md` the single byte source, the two MUST
stay byte-identical so the .md substrate is a faithful mirror of what
the running cycle actually sends. This test is the enforcing contract.

Failure mode: a maintainer edits one but not the other — the .md
substrate drifts from the live wire protocol, and teammates spawned
under the future loader rewire receive a slightly-different reply
contract than teammates spawned today. F7 / GAP-7 drift is the most
dangerous regression vector in team mode because it deadlocks
TeamDelete and silently breaks the SendMessage-to-team-lead reply
expectation.

The prompt-engineer-1 cycle-3 hard rule (Phase 3 Mesh): the F7 line
text is byte-frozen — no normalization, no f-string interpolation, no
"tidy-up" touches the trailer text. This test is what makes that rule
mechanically enforced.
"""

from __future__ import annotations

import re
from pathlib import Path

from scripts.dispatch_templates import TEAMMATE_REPLY_RULE

_TRAILER_PATH = (
    Path(__file__).resolve().parent.parent / "internal" / "cycle" / "templates" / "_trailer.md"
)

# Matches an HTML comment block including any newlines inside it. Using
# DOTALL so multi-line `<!-- ... -->` blocks are removed in a single
# pass. Non-greedy `.*?` so adjacent comment blocks are stripped
# separately (not merged into one giant match).
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _trailer_body() -> str:
    """Read `_trailer.md`, strip ALL HTML comments (including the header
    docstring + the `<!--vars: ... -->` frontmatter), and strip leading/
    trailing whitespace. The result is the rendered body of the partial.
    """
    raw = _TRAILER_PATH.read_text()
    return _HTML_COMMENT_RE.sub("", raw).strip()


def test_trailer_md_body_equals_teammate_reply_rule_byte_for_byte():
    """`_trailer.md` (HTML-comments stripped, whitespace stripped) MUST
    be byte-identical to `TEAMMATE_REPLY_RULE.strip()`.

    Failure message points at BOTH source paths so the fixer knows which
    file to align with which. The `.strip()` on TEAMMATE_REPLY_RULE
    removes its leading `\\n\\n` paragraph separator (the constant is
    designed to be APPENDED to a rendered body; the partial is rendered
    standalone so the leading break has no meaning).
    """
    md_body = _trailer_body()
    py_body = TEAMMATE_REPLY_RULE.strip()
    if md_body != py_body:
        # On mismatch, surface the smallest unit of divergence so the
        # fixer doesn't have to diff two ~1500-byte strings by eye.
        # Find the first differing character index.
        common_len = min(len(md_body), len(py_body))
        first_diff = next(
            (i for i in range(common_len) if md_body[i] != py_body[i]),
            common_len,
        )
        ctx_lo = max(0, first_diff - 40)
        ctx_hi = first_diff + 40
        raise AssertionError(
            "F7/GAP-7 trailer drift between `_trailer.md` and "
            "`scripts.dispatch_templates.TEAMMATE_REPLY_RULE`. "
            f"Files:\n  md: {_TRAILER_PATH}\n  py: scripts/dispatch_templates.py::TEAMMATE_REPLY_RULE\n"
            f"First divergence at byte {first_diff} "
            f"(md_len={len(md_body)}, py_len={len(py_body)}). "
            f"md[{ctx_lo}:{ctx_hi}] = {md_body[ctx_lo:ctx_hi]!r}; "
            f"py[{ctx_lo}:{ctx_hi}] = {py_body[ctx_lo:ctx_hi]!r}. "
            "Per prompt-engineer-1 cycle-3 Phase-3 Mesh rule the F7 "
            "reply-contract text is byte-frozen — fix by aligning "
            "whichever side drifted with the other (the Python constant "
            "is the live wire-protocol source today; AI-3 will swap "
            "that direction once the loader rewire lands)."
        )
