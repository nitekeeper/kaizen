"""Token-meter scenario — the workload value type + loader (Cycle-2).

A :class:`Scenario` is the fixed, comparable workload kaizen measures a target
skill/plugin with: the same prompt is replayed BEFORE the improvement (baseline)
and AFTER it, so the delta is attributable to the edit and nothing else. It pairs
with the dynamic runner (:mod:`scripts.tokenmeter_run`) which replays the prompt
N times and harvests the four-category usage.

Design contract (kaizen token-usage benchmark spec §3 — pluggable source, fixed
measurement):

* ``name`` — a human label for the scenario file.
* ``target`` — the skill/plugin directory the scenario exercises (a repo-relative
  path such as ``skills/improve`` or ``internal/cycle``). The static footprint is
  measured on this path; the dynamic prompt exercises the skill it names.
* ``prompt`` — the representative invocation that drives the target through a
  realistic unit of work.
* ``scenario_hash`` — a STABLE 16-hex digest of ``prompt`` + ``target`` (NOT the
  target's *contents*). This is the comparability control: the schema's
  control-vector gate refuses a before/after delta unless the two reports share a
  scenario_hash, so changing the prompt or the target path (a different scenario)
  is correctly refused, while improving the target's *contents* — same path, same
  prompt — keeps the hash stable and the comparison legitimate. The improved
  version is distinguished by ``metadata.target_commit``, which is NOT a control.
* ``source`` — ``user`` (a hand-written ``benchmark/scenarios/<name>.json``) or
  ``auto`` (synthesized from the target's docs); flows to ``scenario_source``.
* ``cycles`` / ``subject`` — optional run descriptors carried into the report
  metadata so the header pins the comparison context.

The loader reads a user-supplied JSON file (``benchmark/scenarios/<name>.json``)
or accepts inline construction via :meth:`Scenario.create`. The hash is ALWAYS
recomputed from ``prompt`` + ``target`` so two scenarios that name the same work
hash identically regardless of any stale stored value.

SECURITY: the scenario JSON is target-adjacent DATA. It is parsed with
``json.loads`` only — no ``eval`` / ``exec`` / shell. The ``prompt`` is later
handed to the runner as a single ``argv`` element (never a shell string).
Stdlib-only; frozen dataclass.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Scenario source labels (flow into the report's ``scenario_source`` metadata).
SOURCE_USER = "user"
SOURCE_AUTO = "auto"
VALID_SOURCES = (SOURCE_USER, SOURCE_AUTO)

#: NUL separator between target + prompt in the hash pre-image. A byte that
#: cannot appear in either field, so ``(target, prompt)`` can never collide with
#: a different split of the same concatenation.
_HASH_SEP = "\x00"

#: Width of the stable scenario digest (hex chars). Matches the WIDTH of the schema
#: layer's own ``_scenario_hash`` so the two digests are the same length — but they
#: are NOT value-comparable: this Scenario hash covers ``target`` + ``prompt`` while
#: the schema fallback hashes only the ``scenario_source`` label, so equal inputs to
#: one are unrelated to the other. In practice the Scenario's precomputed hash is
#: always threaded into ``metadata.scenario_hash`` (see
#: ``tokenmeter_run._build_metadata``), so the schema's ``_scenario_hash`` fallback
#: only fires when no Scenario hash was supplied; the control-vector gate then
#: compares two Scenario hashes (or two schema fallbacks), never one of each.
_HASH_WIDTH = 16


def compute_scenario_hash(prompt: str, target: str) -> str:
    """Stable 16-hex digest of ``prompt`` + ``target`` (the comparability key).

    Deterministic and content-INDEPENDENT: it hashes the prompt and the target
    PATH, never the target's file contents, so improving the target keeps the
    hash stable (that is the whole point — a before/after pair must share it).
    """
    pre_image = f"{target}{_HASH_SEP}{prompt}".encode()
    return hashlib.sha256(pre_image).hexdigest()[:_HASH_WIDTH]


@dataclass(frozen=True)
class Scenario:
    """A fixed, comparable benchmark workload (frozen/immutable).

    Build one from a JSON file with :func:`load_scenario` or inline with
    :meth:`create` (which computes the stable ``scenario_hash`` for you).
    """

    name: str
    target: str
    prompt: str
    scenario_hash: str
    source: str = SOURCE_USER
    cycles: int = 0
    subject: str = ""

    @classmethod
    def create(
        cls,
        *,
        name: str,
        target: str,
        prompt: str,
        source: str = SOURCE_USER,
        cycles: int = 0,
        subject: str = "",
    ) -> Scenario:
        """Construct a :class:`Scenario`, computing the stable ``scenario_hash``.

        Raises :class:`ValueError` on an empty ``name`` / ``target`` / ``prompt``
        or an unknown ``source`` — a malformed scenario is a setup error that must
        fail loudly rather than silently measure the wrong thing.
        """
        name = (name or "").strip()
        target = (target or "").strip()
        prompt = (prompt or "").strip()
        if not name:
            raise ValueError("scenario 'name' must be a non-empty string")
        if not target:
            raise ValueError("scenario 'target' must be a non-empty string")
        if not prompt:
            raise ValueError("scenario 'prompt' must be a non-empty string")
        if source not in VALID_SOURCES:
            raise ValueError(f"scenario 'source' must be one of {VALID_SOURCES}, got {source!r}")
        return cls(
            name=name,
            target=target,
            prompt=prompt,
            scenario_hash=compute_scenario_hash(prompt, target),
            source=source,
            cycles=int(cycles or 0),
            subject=subject or "",
        )

    def resolve_target(self, repo_root: str | Path | None = None) -> Path:
        """Resolve ``target`` to a filesystem path (against ``repo_root`` or CWD).

        The static footprint is measured on this path. An absolute ``target`` is
        returned verbatim; a relative one resolves against ``repo_root`` (default:
        the current working directory, which is the clone root for a kaizen run).
        """
        target = Path(self.target)
        if target.is_absolute():
            return target
        base = Path(repo_root) if repo_root is not None else Path.cwd()
        return base / target


def from_mapping(data: Any) -> Scenario:
    """Build a :class:`Scenario` from an already-decoded mapping (inline path).

    Reads ``name`` / ``target`` / ``prompt`` (required) and ``source`` / ``cycles``
    / ``subject`` (optional). Any stored ``scenario_hash`` is IGNORED — the hash is
    recomputed from ``prompt`` + ``target`` so it can never drift from the fields it
    summarizes. Raises :class:`ValueError` on a non-object or a missing required
    field.
    """
    if not isinstance(data, dict):
        raise ValueError(f"scenario must be a JSON object, got {type(data).__name__}")
    return Scenario.create(
        name=str(data.get("name", "")),
        target=str(data.get("target", "")),
        prompt=str(data.get("prompt", "")),
        source=str(data.get("source", SOURCE_USER)),
        cycles=int(data.get("cycles", 0) or 0),
        subject=str(data.get("subject", "")),
    )


def load_scenario(path: str | Path) -> Scenario:
    """Load a user-supplied ``benchmark/scenarios/<name>.json`` into a Scenario.

    The file is read as DATA (``json.loads`` only). Raises
    :class:`FileNotFoundError` if absent and :class:`ValueError` on malformed JSON
    or a missing required field (delegated to :func:`from_mapping`).
    """
    text = Path(path).read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"scenario file {path} is not valid JSON: {exc}") from exc
    return from_mapping(data)
