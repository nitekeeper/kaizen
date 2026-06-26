"""Deterministic static footprint of a target skill/plugin's injected context.

Stdlib-only (mirrors the ``scripts/detect_config.py`` idiom: pure functions,
no prompts, callers handle any I/O). This module answers a single question
*without ever loading the model at runtime*: how many tokens does a skill cost
the context window, split by **when** each chunk is paid for?

Three tiers (see ``TIERS``):

* ``passive`` — the SKILL.md YAML ``description`` frontmatter. This is the
  always-resident skill-list entry: every session pays for it whether or not
  the skill is ever invoked.
* ``active-core`` — the SKILL.md body. Paid when the skill is actually invoked.
* ``active-referenced`` — files the SKILL.md body *names* (``scripts/*.py``,
  ``internal/*/SKILL.md``, ``templates/*.md``) plus the plugin's
  ``.claude-plugin/{plugin,marketplace}.json`` metadata. These are a
  *conditional* load: enumerated by a STATIC path-regex SCAN of the body —
  never by executing it — because they are read on demand, not eagerly.

Token counting goes through a :class:`TokenCounter`. The default is a
deterministic character approximation (``char_count / APPROX_DIVISOR``) tagged
``source='approximated'``. An OPTIONAL exact path uses the Anthropic
``/v1/messages/count_tokens`` HTTP endpoint (tagged ``source='measured'``) but
ONLY when ``ANTHROPIC_API_KEY`` is set and the endpoint is reachable; ANY
failure degrades silently to the approximation. There is NO hard dependency on
the network, and this module never raises on a counting failure.

NOTE: the local ``claude`` CLI has no count-tokens subcommand; the measured
path is the HTTP API only. We never shell out to it.

Static figures are INPUT-SIDE only. Output / cache-read / cache-write columns
are rendered as the :data:`NA` marker — never ``0``, because ``0`` would imply
a measured zero rather than "not applicable to a static estimate".

Security note: target SKILL.md / plugin files are tokenized as DATA. Their
contents are measured, never interpreted as instructions.
"""

from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

# The TokenCounter Protocol is owned by scripts/tokenmeter_model.py. Import it
# when available so static and model footprints share one contract; fall back to
# a structurally-identical local Protocol so this module is self-contained and
# importable on its own (the two are duck-compatible — Protocol is structural).
if TYPE_CHECKING:  # pragma: no cover - typing only
    from scripts.tokenmeter_model import TokenCounter
else:
    try:
        from scripts.tokenmeter_model import TokenCounter
    except ImportError:

        @runtime_checkable
        class TokenCounter(Protocol):
            """Counts tokens for a string, or returns None if it cannot."""

            def count(self, text: str) -> int | None: ...


# ── Named, auditable constants ───────────────────────────────────────────────

#: Characters-per-token used by the deterministic approximation. ~4 chars/token
#: is the standard rule-of-thumb for English + code; named so the heuristic is
#: auditable rather than a magic number sprinkled inline.
APPROX_DIVISOR = 4.0

#: Marker for columns that do not apply to a static, input-side estimate.
#: NEVER use 0 here — 0 implies a measured zero, this means "not applicable".
NA = "n/a"

#: Mode tag stamped on every footprint row/total.
MODE = "static"

#: Source tags.
SOURCE_APPROX = "approximated"
SOURCE_MEASURED = "measured"

#: Tier names, in payment order (always-resident → invoked → conditional).
TIER_PASSIVE = "passive"
TIER_ACTIVE_CORE = "active-core"
TIER_ACTIVE_REFERENCED = "active-referenced"
TIERS = (TIER_PASSIVE, TIER_ACTIVE_CORE, TIER_ACTIVE_REFERENCED)

#: Anthropic token-counting endpoint (measured path only).
_COUNT_TOKENS_URL = "https://api.anthropic.com/v1/messages/count_tokens"
_ANTHROPIC_VERSION = "2023-06-01"
_DEFAULT_MODEL = "claude-opus-4-20250514"
_HTTP_TIMEOUT_S = 10.0

#: Static path-regex patterns for the conditional-load scan of a SKILL.md body.
#: These SCAN for referenced paths; they never execute or follow anything.
_REFERENCE_PATTERNS = (
    re.compile(r"scripts/[\w./-]+\.py"),
    re.compile(r"internal/[\w./-]+/SKILL\.md"),
    re.compile(r"templates/[\w./-]+\.md"),
)

#: Relative plugin-metadata paths included in the active-referenced tier.
_PLUGIN_METADATA = (
    ".claude-plugin/plugin.json",
    ".claude-plugin/marketplace.json",
)


# ── Token counters ───────────────────────────────────────────────────────────


def approximate_tokens(text: str) -> int:
    """Deterministic char/``APPROX_DIVISOR`` token estimate (>= 0)."""
    if not text:
        return 0
    return math.ceil(len(text) / APPROX_DIVISOR)


class ApproxTokenCounter:
    """Default counter: deterministic character approximation, never fails."""

    source = SOURCE_APPROX

    def count(self, text: str) -> int:
        return approximate_tokens(text)


class AnthropicTokenCounter:
    """Optional exact counter via the Anthropic count_tokens HTTP endpoint.

    Returns ``None`` on ANY failure (no key, unreachable, bad response) so the
    caller falls back to approximation. Never raises. The measured path is the
    HTTP API only — we do NOT shell to the local ``claude`` CLI.
    """

    source = SOURCE_MEASURED

    def __init__(
        self,
        api_key: str | None = None,
        model: str = _DEFAULT_MODEL,
        timeout: float = _HTTP_TIMEOUT_S,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY")
        self.model = model
        self.timeout = timeout

    def available(self) -> bool:
        """True only if an API key is present (necessary, not sufficient)."""
        return bool(self.api_key)

    def count(self, text: str) -> int | None:
        if not self.api_key:
            return None
        if not text:
            return 0
        payload = json.dumps(
            {
                "model": self.model,
                "messages": [{"role": "user", "content": text}],
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            _COUNT_TOKENS_URL,
            data=payload,
            method="POST",
            headers={
                "content-type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": _ANTHROPIC_VERSION,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # nosec B310
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            return None
        tokens = body.get("input_tokens")
        if isinstance(tokens, int):
            return tokens
        return None


def _count_text(text: str, tokenizer: TokenCounter | None) -> tuple[int, str]:
    """Count ``text`` with ``tokenizer``, falling back to approximation.

    Returns ``(token_count, source)``. Any tokenizer that returns ``None`` (or
    has no usable ``count``) degrades to the deterministic approximation. Never
    raises.
    """
    if tokenizer is not None:
        try:
            result = tokenizer.count(text)
        except Exception:  # a counter must never break the meter
            result = None
        if result is not None:
            source = getattr(tokenizer, "source", SOURCE_MEASURED)
            return int(result), str(source)
    return approximate_tokens(text), SOURCE_APPROX


# ── Static enumeration (scan, never execute) ─────────────────────────────────


def _find_repo_root(skill_dir: Path) -> Path:
    """Walk up from ``skill_dir`` to the plugin/project root.

    First ancestor (inclusive) containing ``.claude-plugin`` or
    ``pyproject.toml`` wins; otherwise fall back two levels up
    (``skills/<name>/`` → repo root) or the filesystem anchor.
    """
    skill_dir = Path(skill_dir)
    for candidate in (skill_dir, *skill_dir.parents):
        if (candidate / ".claude-plugin").exists() or (candidate / "pyproject.toml").exists():
            return candidate
    parents = skill_dir.parents
    if len(parents) >= 2:
        return parents[1]
    return skill_dir


def split_frontmatter(text: str) -> tuple[str, str]:
    """Split a SKILL.md into ``(frontmatter_block, body)``.

    Frontmatter is the content between a leading ``---`` line and the next
    ``---`` line. If there is no frontmatter, returns ``("", text)``.
    """
    if not text.startswith("---"):
        return "", text
    match = re.match(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?(.*)\Z", text, re.DOTALL)
    if not match:
        return "", text
    return match.group(1), match.group(2)


def extract_description(frontmatter: str) -> str:
    """Extract the YAML ``description`` value from a frontmatter block.

    Minimal, dependency-free YAML handling: supports an inline scalar
    (``description: ...``) and folded/literal block scalars (``description: >``
    or ``|`` followed by indented lines). Surrounding quotes are stripped.
    Returns ``""`` when absent.
    """
    lines = frontmatter.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^description:[ \t]*(.*)$", line)
        if not match:
            continue
        inline = match.group(1).strip()
        if inline and inline not in (">", "|", ">-", "|-", ">+", "|+"):
            return _strip_quotes(inline)
        # Block scalar: gather subsequent indented lines.
        collected: list[str] = []
        for follow in lines[index + 1 :]:
            if follow.strip() == "":
                collected.append("")
                continue
            if follow[:1] in (" ", "\t"):
                collected.append(follow.strip())
            else:
                break
        return " ".join(part for part in collected if part).strip()
    return ""


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def scan_referenced_paths(body: str) -> list[str]:
    """Statically scan a SKILL.md body for referenced relative paths.

    Returns a sorted, de-duplicated list of repo-relative paths matching the
    conditional-load patterns (``scripts/*.py``, ``internal/*/SKILL.md``,
    ``templates/*.md``). This SCANS the text — it never opens or executes it.
    """
    found: set[str] = set()
    for pattern in _REFERENCE_PATTERNS:
        found.update(pattern.findall(body))
    return sorted(found)


def _read_text(path: Path) -> str:
    """Read a file as DATA (never interpreted). ``""`` if unreadable."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def enumerate_footprint(skill_dir: str | Path) -> list[dict]:
    """Enumerate the static footprint of ``skill_dir`` across the three tiers.

    Returns an ordered list of ``{"path", "tier", "text"}`` records. No token
    counting happens here — that is :func:`static_footprint`'s job. Referenced
    files and plugin metadata are discovered by SCANNING (regex over the body,
    fixed metadata paths); nothing is executed and only existing files are
    included (conditional load).
    """
    skill_dir = Path(skill_dir)
    repo_root = _find_repo_root(skill_dir)
    records: list[dict] = []

    skill_md = skill_dir / "SKILL.md"
    body = ""
    if skill_md.is_file():
        raw = _read_text(skill_md)
        frontmatter, body = split_frontmatter(raw)
        description = extract_description(frontmatter)
        rel_skill = _rel(skill_md, repo_root)
        records.append(
            {
                "path": f"{rel_skill}::frontmatter.description",
                "tier": TIER_PASSIVE,
                "text": description,
            }
        )
        records.append({"path": rel_skill, "tier": TIER_ACTIVE_CORE, "text": body})

    seen: set[str] = set()
    for rel in scan_referenced_paths(body):
        target = repo_root / rel
        if target.is_file() and rel not in seen:
            seen.add(rel)
            records.append(
                {"path": rel, "tier": TIER_ACTIVE_REFERENCED, "text": _read_text(target)}
            )

    for rel in _PLUGIN_METADATA:
        target = repo_root / rel
        if target.is_file() and rel not in seen:
            seen.add(rel)
            records.append(
                {"path": rel, "tier": TIER_ACTIVE_REFERENCED, "text": _read_text(target)}
            )

    return records


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


# ── Public footprint API ─────────────────────────────────────────────────────


def static_footprint(skill_dir: str | Path, tokenizer: TokenCounter | None = None) -> dict:
    """Compute the per-file + tiered static token footprint of a skill/plugin.

    ``tokenizer`` is any object with a ``count(text) -> int | None`` method (the
    :class:`TokenCounter` contract). When ``None`` (the default) or when it
    returns ``None``, the deterministic char-approximation is used. The measured
    path is opt-in and best-effort; this function never raises on counting.

    Returns a dict::

        {
          "mode": "static",
          "files": [
            {"path", "tier", "char_count", "token_count", "source",
             "input", "output", "cache_read", "cache_write"},
            ...
          ],
          "tiers": {tier: {"char_count", "token_count", "file_count"}, ...},
          "totals": {"char_count", "token_count", "file_count"},
        }

    Static figures are INPUT-SIDE only: ``input`` carries the token count while
    ``output`` / ``cache_read`` / ``cache_write`` are the :data:`NA` marker
    (never ``0``).
    """
    records = enumerate_footprint(skill_dir)

    files: list[dict] = []
    tiers: dict[str, dict] = {
        tier: {"char_count": 0, "token_count": 0, "file_count": 0}  # nosec B105
        for tier in TIERS
    }
    total_chars = 0
    total_tokens = 0

    for record in records:
        text = record["text"]
        char_count = len(text)
        token_count, source = _count_text(text, tokenizer)
        tier = record["tier"]

        files.append(
            {
                "path": record["path"],
                "tier": tier,
                "char_count": char_count,
                "token_count": token_count,
                "source": source,
                # Input-side only — never emit 0 for the non-applicable columns.
                "input": token_count,
                "output": NA,
                "cache_read": NA,
                "cache_write": NA,
            }
        )

        bucket = tiers.setdefault(
            tier,
            {"char_count": 0, "token_count": 0, "file_count": 0},  # nosec B105
        )
        bucket["char_count"] += char_count
        bucket["token_count"] += token_count
        bucket["file_count"] += 1
        total_chars += char_count
        total_tokens += token_count

    return {
        "mode": MODE,
        "files": files,
        "tiers": tiers,
        "totals": {
            "char_count": total_chars,
            "token_count": total_tokens,
            "file_count": len(files),
        },
    }


def default_tokenizer() -> TokenCounter:
    """Pick the best available counter: measured if a key is set, else approx.

    Always returns a usable counter — the Anthropic counter self-degrades to
    ``None`` per-call on any failure, which :func:`static_footprint` then turns
    into an approximation. Never raises.
    """
    anthropic = AnthropicTokenCounter()
    if anthropic.available():
        return anthropic
    return ApproxTokenCounter()


if __name__ == "__main__":  # pragma: no cover - manual smoke entry point
    import argparse

    parser = argparse.ArgumentParser(description="Static token footprint of a skill/plugin.")
    parser.add_argument("skill_dir", help="Path to the skill directory (containing SKILL.md).")
    parser.add_argument(
        "--measured",
        action="store_true",
        help="Use the Anthropic count_tokens HTTP endpoint when ANTHROPIC_API_KEY is set.",
    )
    parsed = parser.parse_args()
    chosen = default_tokenizer() if parsed.measured else ApproxTokenCounter()
    print(json.dumps(static_footprint(parsed.skill_dir, tokenizer=chosen), indent=2))
