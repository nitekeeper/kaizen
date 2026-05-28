"""Soft-drop synthesis-template contract tests (#83).

The per-phase quorum marks a silent teammate's slot with a response that
opens with ``SOFT_DROP_SENTINEL``. The Phase-3 synthesis templates carry a
clause telling the synthesising agent to treat such a slot as ABSENT (never
assent) and not to fabricate the missing teammate's position.

These tests pin the template<->constant contract so the literal sentinel in
the markdown cannot silently drift from ``bridge_softdrop.SOFT_DROP_SENTINEL``.
"""

from __future__ import annotations

from pathlib import Path

from scripts.bridge_softdrop import SOFT_DROP_SENTINEL
from scripts.dispatch_templates import phase_3_close, phase_3_open

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "internal" / "cycle" / "templates"
_PARTIAL = _TEMPLATE_DIR / "_soft_drop_absent.md"


def test_soft_drop_partial_exists_and_pins_the_sentinel():
    assert _PARTIAL.is_file(), "shared soft-drop partial is missing"
    body = _PARTIAL.read_text(encoding="utf-8")
    # The literal in the partial MUST equal the authoritative constant.
    assert SOFT_DROP_SENTINEL in body
    assert "ABSENT" in body


def test_phase_3_open_renders_soft_drop_absent_clause():
    rendered = phase_3_open(proposals=[{"agent": "x", "raw": "hi"}])
    assert SOFT_DROP_SENTINEL in rendered
    assert "ABSENT" in rendered
    # The clause must forbid fabricating the absent teammate's position.
    assert "fabricate" in rendered.lower()


def test_phase_3_close_renders_soft_drop_absent_clause():
    rendered = phase_3_close(
        proposals=[{"agent": "x", "raw": "hi"}],
        agreements=[{"agent": "x", "raw": "ok"}],
    )
    assert SOFT_DROP_SENTINEL in rendered
    assert "ABSENT" in rendered
