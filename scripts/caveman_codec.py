"""Caveman prose compressor — shrink free-text without losing technical fidelity.

This is kaizen's port of the caveman-shrink ``withProtectedSegments`` pattern
(see the caveman plugin's ``src/mcp-servers/caveman-shrink/compress.js``). The
motto is "talk like caveman, brain stays big": the prose gets terser to save
tokens, but every byte of technical content survives intact.

The codec is **deterministic, stdlib-only, and idempotent**. It NEVER touches a
protected segment (code, URLs, paths, identifiers, version numbers, quoted
error-strings) and it refuses to compress text whose meaning could flip under
compression (security / destructive / multi-step content — see
:func:`should_compress`).

Public API
----------
- :func:`compress` — ``compress(text, level="full") -> str``
- :func:`should_compress` — auto-clarity gate; ``False`` ⇒ caller must pass the
  text through verbatim (do NOT compress).
- :data:`LEVELS` — the supported level names (``"off"``, ``"lite"``, ``"full"``).

Mechanism (mirrors the JS reference)
-------------------------------------
1. Mask every protected segment to a numbered sentinel BEFORE any stripping,
   restore them AFTER. Protected = MUST survive byte-identical.
2. Strip stopwords / filler / hedging / pleasantries on the UNPROTECTED prose,
   per level.
3. Collapse whitespace introduced by removals and tidy sentence casing.

Idempotence: re-compressing already-compressed text is a fixed point —
``compress(compress(x)) == compress(x)`` for every level.

SECURITY / boundary note: this module is intentionally a *prose* compressor for
ORCHESTRATOR-FACING / persisted free-text only. It MUST NOT be applied upstream
of any byte-sensitive parser (see ``scripts/team_executor.py`` parsers and the
AI-3 wiring in :func:`team_executor.compress_reply_for_context`).
"""

from __future__ import annotations

import re

# ── Supported levels ──────────────────────────────────────────────────────
#
# "off"  — identity (return input unchanged).
# "lite" — drop filler + hedging only; KEEP articles + full sentences.
# "full" — drop articles + filler + hedging + pleasantries + leading
#          person-phrases; collapse whitespace. Classic caveman.
LEVELS: tuple[str, ...] = ("off", "lite", "full")
DEFAULT_LEVEL = "full"


# ── Stopword / stripping patterns ─────────────────────────────────────────
#
# All matching is case-insensitive (``re.I``). ``\b`` word-boundaries keep us
# from clipping substrings of longer words (e.g. "thethe" or "varticle").

_FILLERS = re.compile(
    r"\b(?:just|really|basically|actually|simply|quite|very|essentially|literally)\b",
    re.I,
)

_PLEASANTRIES = re.compile(
    r"\b(?:please|kindly|thank you|thanks|sure|certainly|of course|happy to|"
    r"i'?d be happy)\b[,.]?[ \t]*",
    re.I,
)

_HEDGES = re.compile(
    r"\b(?:perhaps|maybe|might|could potentially|would like to|i think|"
    r"in my opinion|it seems|it appears)\b[ \t]*",
    re.I,
)

# Leading person-phrases — only stripped at the very start of a line (``re.M``
# so each line's leading phrase is caught, mirroring the JS LEADERS pattern).
_LEADERS = re.compile(
    r"^(?:i'?ll|i will|i can|i'?d|you can|we will|we can|let me|let'?s)[ \t]+",
    re.I | re.M,
)

# Articles — only when followed by a lowercase word (so we never eat a leading
# article of a Proper Noun or a protected sentinel, which is a digit). The JS
# reference uses the same ``(?=[a-z])`` lookahead.
_ARTICLES = re.compile(r"\b(?:a|an|the)\s+(?=[a-z])", re.I)

# M7 — anchored head detectors: does the text (after optional leading
# whitespace) BEGIN with a stopword that this level would strip? Used to decide
# whether re-capitalizing the first letter is warranted. ``full`` strips
# leaders/pleasantries/articles too; ``lite`` only strips filler/hedging.
_HEAD_FULL_RE = re.compile(
    r"^[ \t]*(?:"
    r"i'?ll|i will|i can|i'?d|you can|we will|we can|let me|let'?s|"  # leaders
    r"please|kindly|thank you|thanks|sure|certainly|of course|happy to|i'?d be happy|"  # pleasantries
    r"perhaps|maybe|might|could potentially|would like to|i think|in my opinion|"
    r"it seems|it appears|"  # hedges
    r"just|really|basically|actually|simply|quite|very|essentially|literally|"  # fillers
    r"a|an|the"  # articles
    r")\b",
    re.I,
)
_HEAD_LITE_RE = re.compile(
    r"^[ \t]*(?:"
    r"perhaps|maybe|might|could potentially|would like to|i think|in my opinion|"
    r"it seems|it appears|"  # hedges
    r"just|really|basically|actually|simply|quite|very|essentially|literally"  # fillers
    r")\b",
    re.I,
)


def _head_starts_with_stopword(text: str, level: str) -> bool:
    """True if ``text`` begins (after leading whitespace) with a stopword that
    ``level`` strips — i.e. the first token will be removed, so the NEW first
    token should be sentence-cased. ``article``-before-non-lowercase is the one
    nuance: ``_ARTICLES`` only fires before a lowercase word, but the head
    detector is intentionally permissive here (a head article is virtually
    always followed by a lowercase word in real prose; the worst case is a
    spurious capitalization of an already-capitalized first letter, a no-op)."""
    rx = _HEAD_FULL_RE if level == "full" else _HEAD_LITE_RE
    return rx.match(text) is not None


# ── Protected-segment patterns (order matters — broadest structural first) ──
#
# Each pattern, in order, is masked to a sentinel before any prose stripping
# runs and restored verbatim afterward. The ordering means fenced/inline code
# is masked before path/identifier patterns get a chance to nibble at code
# innards.
_PROTECTED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"```[\s\S]*?```"),  # fenced code blocks
    re.compile(r"`[^`\n]+`"),  # inline code
    re.compile(r"\bhttps?://\S+", re.I),  # http(s) URLs
    re.compile(r"\bgit@[\w.-]+:[\w./-]+"),  # git@host:path SCP-style URLs
    # quoted error-string-like tokens: a "..." or '...' span. Masked early so
    # filler INSIDE an error string is preserved byte-identical.
    re.compile(r"\"[^\"\n]*\"|'[^'\n]*'"),
    # filesystem paths — any token containing `/` or `\`. Anchored on a
    # word/dot/dash run so it grabs the whole path token, not just the slash.
    re.compile(r"[\w.\-~]*[\\/][\w.\\/\-~]+"),
    re.compile(r"\b[A-Z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b"),  # CONST_CASE
    re.compile(r"\b\w+(?:\.\w+)+\([^)]*\)?|\b\w+(?:\.\w+)+"),  # dotted.id / pkg.fn()
    re.compile(r"\b[A-Za-z_]\w*\([^)]*\)"),  # function calls fn(...)
    re.compile(r"\bv?\d+\.\d+(?:\.\d+)*(?:[-+][\w.]+)?\b"),  # semver / version-like
)

# Sentinel format: a NUL-delimited index so it can never collide with anything
# the prose stripping produces (NUL never appears in legit prose) and so the
# articles/leaders patterns (which key off [a-z]/word-chars) never touch it.
_SENTINEL_OPEN = "\x00"
_SENTINEL_CLOSE = "\x00"

# Whitespace tidy-up patterns.
_MULTISPACE = re.compile(r"[ \t]{2,}")
_SPACE_BEFORE_PUNCT = re.compile(r"[ \t]+([,.;:!?])")
_TRIPLE_NEWLINE = re.compile(r"\n{3,}")
_SENTENCE_START = re.compile(r"(^|[.!?]\s+)([a-z])")
# Orphaned mid-sentence punctuation left when a trailing filler/pleasantry is
# dropped (e.g. "... change, really." → "... change,." ⇒ "... change."). A
# comma/semicolon/colon immediately before a sentence terminator collapses to
# the terminator. Optional whitespace between the two is also absorbed.
_ORPHAN_PUNCT = re.compile(r"[,;:]+(?=[ \t]*[.!?])")
# A leading orphan punctuation at the very start of a line (a dropped leader
# left a stray comma/period) is removed.
_LEADING_ORPHAN_PUNCT = re.compile(r"^[ \t]*[,;:][ \t]*", re.M)


# ── Auto-clarity tripwires (should_compress → False) ──────────────────────
#
# Conservative: ANY hit means "do not compress; pass through verbatim".
_SECURITY_RE = re.compile(
    r"\b(?:security|vulnerab\w*|cve-\d|exploit|credential|secret|password|"
    r"token leak|injection|warning:|caution:|danger\w*)\b",
    re.I,
)
_DESTRUCTIVE_RE = re.compile(
    # M4 — destructive IMPERATIVE phrases + explicit markers ONLY. Bare verbs
    # (delete / truncate / drop / force-push) are NOT tripwires — "delete the
    # unused import" or "force-push protection looks fine" are ordinary prose
    # we WANT to compress. We trip only on:
    #   - confirmation prompts: ``approve?`` / ``confirm?`` (the trailing ``?``
    #     is matched literally; the trailing ``\b`` is dropped because ``?`` is
    #     a non-word char and would never produce a boundary after it),
    #   - explicit danger markers: ``destructive`` / ``irreversible`` /
    #     ``cannot be undone``,
    #   - real destructive commands: ``rm -rf``, ``DROP TABLE``,
    #     ``DELETE FROM`` (SQL row-purge), ``TRUNCATE TABLE``,
    #     ``git push --force`` / ``git push -f``, ``git reset --hard``,
    #     ``git clean -fd`` (and other ``-f`` clean variants), ``mkfs``, and
    #     the classic fork bomb ``:(){ :|:& };:``.
    # N2 — the SQL/git destructive set is the IMPERATIVE form: ``delete from`` /
    # ``truncate table`` / ``git clean -<...>f`` (not the bare verbs), so
    # "delete the unused import" still compresses while "DELETE FROM users"
    # does not.
    r"\b(?:approve|confirm)\?"
    r"|\b(?:destructive|irreversible|cannot be undone)\b"
    r"|\brm\s+-[a-z]*r[a-z]*f|\brm\s+-[a-z]*f[a-z]*r"
    r"|\bdrop\s+table\b"
    r"|\bdelete\s+from\b"
    r"|\btruncate\s+table\b"
    r"|\bgit\s+push\s+(?:--force\b|-f\b)"
    r"|\bgit\s+reset\s+--hard\b"
    r"|\bgit\s+clean\s+-[a-z]*f"
    r"|\bmkfs\b"
    r"|:\(\)\s*\{",
    re.I,
)
# A numbered multi-step sequence: ≥3 lines whose first non-space chars are
# ``N.`` or ``N)``. Dropping conjunctions/articles across ordered steps can
# flip meaning, so we never compress these.
_STEP_LINE_RE = re.compile(r"^\s*\d+[.)]\s+\S", re.M)
_MIN_STEPS = 3


def _mask_protected(text: str) -> tuple[str, list[str]]:
    """Replace every protected match with a ``\\x00<idx>\\x00`` sentinel.

    Returns ``(working_text, segments)`` where ``segments[i]`` is the original
    byte-span that sentinel ``i`` stands for. Surrounding spaces are added
    around the sentinel so an adjacent stopword (e.g. ``the`` before a path)
    still word-boundary-matches and the restore puts the original back without
    fusing tokens.

    M5 — sentinel collision is impossible because the SOLE caller
    (:func:`compress`) strips every literal NUL from ``text`` BEFORE calling
    this function. The sentinel delimiter is NUL (``\\x00``), so a stripped
    input can never contain a ``\\x00<digits>\\x00`` span that the restore
    pass would mistake for a sentinel we issued.
    """
    segments: list[str] = []
    working = text

    def _sub(m: re.Match[str]) -> str:
        idx = len(segments)
        segments.append(m.group(0))
        return f" {_SENTINEL_OPEN}{idx}{_SENTINEL_CLOSE} "

    for pattern in _PROTECTED_PATTERNS:
        working = pattern.sub(_sub, working)
    return working, segments


def _restore_protected(text: str, segments: list[str]) -> str:
    """Splice the original protected spans back in place of their sentinels.

    Only indices we actually issued (``0 <= i < len(segments)``) are resolved;
    any other ``\\x00<digits>\\x00`` span is left VERBATIM (defence-in-depth on
    top of the NUL-strip in :func:`compress` — an out-of-range index never
    IndexErrors, it just passes through). The one padding space added on each
    side at mask time is trimmed so ``foo \\x00N\\x00 bar`` collapses back to
    ``foo <orig> bar`` (single spaces, not double).
    """

    def _restore(m: re.Match[str]) -> str:
        idx = int(m.group(1))
        if 0 <= idx < len(segments):
            return " " + segments[idx] + " "
        # Not a sentinel we issued — leave the literal span untouched (bound
        # check prevents the IndexError that a crafted/echoed \x00N\x00 in the
        # source would otherwise raise).
        return m.group(0)

    text = re.sub(r" ?\x00(\d+)\x00 ?", _restore, text)
    # Collapse any double spaces the padding introduced.
    text = _MULTISPACE.sub(" ", text)
    return text


def _strip_prose(text: str, level: str) -> str:
    """Apply the per-level stopword/whitespace stripping to UNPROTECTED prose.

    ``level`` is one of :data:`LEVELS` (``"off"`` is handled by the caller and
    never reaches here). The stripping order matches the JS reference: leaders,
    pleasantries, hedges, fillers, (articles for ``full``), then whitespace
    tidy-up + sentence-casing.
    """
    s = text
    # M7 — detect whether a leading-position stopword was stripped from the
    # absolute head of the text (a leader / pleasantry / hedge / filler /
    # article whose match starts at index 0, ignoring leading whitespace). Only
    # THEN do we re-capitalize the first letter below; if the head was NOT a
    # stopword we MUST preserve the source's leading case (e.g.
    # "git commit really now" → "git commit now", first token stays "git").
    head_stripped = _head_starts_with_stopword(s, level)
    if level == "full":
        s = _LEADERS.sub("", s)
        s = _PLEASANTRIES.sub("", s)
    s = _HEDGES.sub("", s)
    s = _FILLERS.sub("", s)
    if level == "full":
        s = _ARTICLES.sub("", s)

    # Collapse whitespace introduced by removals.
    s = _MULTISPACE.sub(" ", s)
    s = _SPACE_BEFORE_PUNCT.sub(r"\1", s)
    # Collapse orphaned mid-sentence punctuation left by a dropped trailing
    # filler/pleasantry ("change, really." → "change,." → "change.").
    s = _ORPHAN_PUNCT.sub("", s)
    s = _TRIPLE_NEWLINE.sub("\n\n", s)
    # A dropped leader/article at the very start leaves a leading pad (and
    # possibly a stray comma); remove ONLY the overall leading whitespace so
    # the sentence-start ``^`` anchor sees the real first letter — without
    # disturbing per-line indentation of legitimate multi-line prose (bullets,
    # blockquotes). This is what makes ``full`` idempotent on leader-led text.
    s = s.lstrip(" \t")
    s = _LEADING_ORPHAN_PUNCT.sub("", s)
    # Re-capitalize the first letter ONLY when the head was actually a stripped
    # stopword (M7). The new first word then came from dropping a leading
    # article/leader, so capitalizing it restores normal sentence case; when
    # the head was untouched we leave the source's leading case as-is.
    # Sentinels start with \x00 (not [a-z]) so they are never touched. Only the
    # very first sentence-start (count=1) is re-cased — a mid-text ". word"
    # boundary is left alone since we cannot prove a stopword was dropped there.
    if head_stripped:
        s = _SENTENCE_START.sub(lambda m: m.group(1) + m.group(2).upper(), s, count=1)
    return s


def should_compress(text: str) -> bool:
    """Auto-clarity gate. Return ``False`` when ``text`` MUST pass through verbatim.

    Returns ``False`` (caller skips compression) when ``text`` trips any
    auto-clarity wire:

    - **security** markers (``security``, ``vulnerability``, ``CVE-…``,
      ``warning:``, ``credential``, …),
    - **destructive / confirmation** markers — confirmation prompts
      (``approve?`` / ``confirm?``), explicit danger words (``destructive`` /
      ``irreversible`` / ``cannot be undone``), and destructive IMPERATIVE
      commands (``rm -rf``, ``DROP TABLE``, ``DELETE FROM``, ``TRUNCATE TABLE``,
      ``git push --force`` / ``-f``, ``git reset --hard``, ``git clean -fd``,
      ``mkfs``, fork bomb). M4: BARE verbs (``delete`` / ``truncate`` /
      ``drop`` / ``force-push`` in ordinary prose like "delete the unused
      import") are NOT tripwires — they compress.
    - a **numbered multi-step sequence** of ≥3 steps (``1.`` / ``2.`` / ``3.``)
      where dropping conjunctions/articles could flip meaning.

    Be conservative: non-string / empty input also returns ``False`` (nothing
    to gain, and ``None``/empty is a caller bug we don't want to mask). When
    unsure, ``False``.
    """
    if not isinstance(text, str) or not text.strip():
        return False
    if _SECURITY_RE.search(text):
        return False
    if _DESTRUCTIVE_RE.search(text):
        return False
    # A numbered multi-step sequence of ≥3 steps is NOT compressible (dropping
    # conjunctions/articles across ordered steps can flip meaning).
    return len(_STEP_LINE_RE.findall(text)) < _MIN_STEPS


def compress(text: str, level: str = DEFAULT_LEVEL) -> str:
    """Compress prose ``text`` at ``level``; protected segments survive verbatim.

    Args:
        text: the free-text to compress. Non-string / empty input is returned
            unchanged (identity) so callers never need to pre-guard.
        level: one of :data:`LEVELS`. ``"off"`` returns ``text`` unchanged
            (true identity — same object semantics as input). ``"lite"`` drops
            filler + hedging only (keeps articles + full sentences). ``"full"``
            (default) is classic caveman: also drops articles, pleasantries,
            and leading person-phrases.

    Returns:
        The compressed string. Idempotent for every level:
        ``compress(compress(x, lvl), lvl) == compress(x, lvl)``.

    Raises:
        ValueError: if ``level`` is not in :data:`LEVELS`.

    NOTE: :func:`compress` does NOT consult :func:`should_compress` — the
    auto-clarity gate is the CALLER's responsibility (so callers can log /
    branch on the decision). The AI-3 wiring composes the two.
    """
    if level not in LEVELS:
        raise ValueError(
            f"caveman_codec.compress: unknown level {level!r}; expected one of {LEVELS}"
        )
    if level == "off":
        return text
    if not isinstance(text, str) or text == "":
        return text

    # M5 — the sentinel delimiter is NUL (\x00). Strip every literal NUL from
    # the input BEFORE masking so a crafted/echoed `\x00<digits>\x00` span in
    # the source can never collide with a sentinel we issue (which would
    # otherwise IndexError, or splice the wrong segment, on restore). NUL never
    # appears in legitimate prose, so this strip is loss-free for real text.
    # (`_restore_protected` also bound-checks indices as defence-in-depth.)
    if "\x00" in text:
        text = text.replace("\x00", "")

    working, segments = _mask_protected(text)
    working = _strip_prose(working, level)
    out = _restore_protected(working, segments)
    # Final whitespace tidy — restore may reintroduce a space before punctuation
    # or a leading/trailing pad. Trim ends; keep interior newlines intact.
    out = _SPACE_BEFORE_PUNCT.sub(r"\1", out)
    out = _MULTISPACE.sub(" ", out)
    # Strip trailing spaces on each line + leading/trailing whitespace overall,
    # without collapsing intentional blank lines (handled by _TRIPLE_NEWLINE).
    out = "\n".join(line.rstrip() for line in out.split("\n"))
    return out.strip()
