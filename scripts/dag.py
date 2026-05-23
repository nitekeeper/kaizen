"""Phase 3 Action Items DAG validation gates.

Per internal/synthesis-meeting/SKILL.md lines ~133-139, the synthesis
meeting's output DAG must pass 4 gates before Phase 4 wave dispatch:

  1. ACYCLIC — the depends_on graph has no cycles
  2. NO WITHIN-WAVE FILE CONTENTION — two items in the same wave may
     not both touch the same file
  3. READS SATISFIABLE — every file in a `reads` field must either
     exist in the codebase OR be produced (touched) by an earlier-wave item
  4. NO ORPHAN DEPENDENCIES — every id in a `depends_on` must exist in
     the items list

This module is the single source of truth. Pure functions; no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

# Required keys on every Action Item dict (per internal/synthesis-meeting/SKILL.md
# lines 25-34 + the Action Items table at 114-125 which lists `wave` as a column).
# Used for fail-loud malformed-input detection.
_REQUIRED_KEYS: tuple[str, ...] = ("id", "touches", "reads", "depends_on", "wave")


class DAGValidationError(ValueError):
    """Base class for all DAG validation failures."""


class CycleDetectedError(DAGValidationError):
    """A cycle exists in the depends_on graph."""


class FileContentionError(DAGValidationError):
    """Two items in the same wave touch the same file."""


class UnsatisfiableReadsError(DAGValidationError):
    """An item's `reads` field references a file no earlier wave produces and which doesn't exist in `existing_files`."""


class OrphanDependencyError(DAGValidationError):
    """An item's `depends_on` references an id not in the items list."""


@dataclass(frozen=True)
class ValidationResult:
    """Returned by validate_dag — captures success + the topological waves."""

    ok: bool
    waves: tuple[tuple[str, ...], ...]  # waves[0] is Wave 1 (item ids), waves[1] is Wave 2, etc.
    errors: tuple[DAGValidationError, ...]  # empty if ok=True


def _check_item_shape(items: list[dict]) -> None:
    """Fail loud on malformed Action Item dicts.

    Mirrors cycle 1's fail-loud convention: missing required keys raise
    ValueError immediately; the message names the offending index and
    lists every required key so the agent can fix the source.
    """
    required_list = ", ".join(_REQUIRED_KEYS)
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(
                f"item {idx} is {type(item).__name__}, expected dict; "
                f"required keys: {required_list}"
            )
        for key in _REQUIRED_KEYS:
            if key not in item:
                raise ValueError(
                    f"item {idx} missing required key {key!r}; required: {required_list}"
                )
        # `id` must be a str — an int id would silently propagate through the
        # depends_on graph and produce confusing orphan/cycle messages.
        if not isinstance(item["id"], str):
            raise ValueError(
                f"item {idx} 'id' must be str, got {type(item['id']).__name__}={item['id']!r}"
            )
        # touches / reads / depends_on must be iterables of str. Each element
        # must itself be a str; a stray int element would otherwise silently
        # surface as an OrphanDependencyError (or a meaningless FileContention)
        # and mask the real shape bug.
        for key in ("touches", "reads", "depends_on"):
            value = item[key]
            if not isinstance(value, list | tuple):
                raise ValueError(
                    f"item {idx} key {key!r} is {type(value).__name__}, "
                    f"expected list or tuple of str"
                )
            for elem_idx, elem in enumerate(value):
                if not isinstance(elem, str):
                    raise ValueError(
                        f"item {idx} {key!r}[{elem_idx}] must be str, "
                        f"got {type(elem).__name__}={elem!r}"
                    )
            # Intra-item duplicates are shape bugs, not DAG-gate failures.
            # E.g. touches=['x.py','x.py'] would falsely fire as a wave-2
            # contention 'x.py touched by both A and A'. Reject loudly here.
            if len(value) != len(set(value)):
                seen: set[str] = set()
                dupes: list[str] = []
                for elem in value:
                    if elem in seen and elem not in dupes:
                        dupes.append(elem)
                    seen.add(elem)
                raise ValueError(
                    f"item {idx} {key!r} contains duplicate entries {dupes!r}; "
                    f"full value: {list(value)!r}"
                )


def topological_waves(items: list[dict]) -> tuple[tuple[str, ...], ...]:
    """Compute the topological ordering of Action Items into waves.

    Wave 1 has all items with empty depends_on (in-degree 0).
    Wave N has all items whose longest dependency chain is N-1.

    Raises CycleDetectedError if the graph contains a cycle (with the
    cycle members named in the message).

    Pure function; deterministic on input.
    """
    _check_item_shape(items)

    # id -> item lookup, and the set of all known ids.
    by_id: dict[str, dict] = {}
    for idx, item in enumerate(items):
        item_id = item["id"]
        if item_id in by_id:
            raise ValueError(f"item {idx} has duplicate id {item_id!r}; ids must be unique")
        by_id[item_id] = item
    known_ids = set(by_id)

    # Build in-degree from depends_on edges that point at KNOWN ids only.
    # Orphan deps are surfaced separately by validate_dag (gate 4) — here we
    # silently ignore them so the topological sort still runs and gate 4
    # can report a clean, focused error rather than being shadowed by a cycle.
    in_degree: dict[str, int] = dict.fromkeys(by_id, 0)
    successors: dict[str, list[str]] = {item_id: [] for item_id in by_id}
    for item in items:
        item_id = item["id"]
        for dep in item["depends_on"]:
            if dep in known_ids:
                in_degree[item_id] += 1
                successors[dep].append(item_id)

    # Kahn's algorithm, wave by wave. Preserve input order within each wave
    # for determinism.
    input_order = [item["id"] for item in items]
    remaining = set(by_id)
    waves: list[tuple[str, ...]] = []
    while remaining:
        ready = [
            item_id for item_id in input_order if item_id in remaining and in_degree[item_id] == 0
        ]
        if not ready:
            # Cycle: every remaining node still has at least one unresolved
            # predecessor. Name the members so the PM can negotiate the break.
            cycle_members = sorted(remaining)
            raise CycleDetectedError(
                f"cycle detected in depends_on graph; members (or downstream of cycle): "
                f"{cycle_members}"
            )
        waves.append(tuple(ready))
        for item_id in ready:
            remaining.discard(item_id)
            for succ in successors[item_id]:
                in_degree[succ] -= 1

    return tuple(waves)


def validate_dag(
    items: list[dict],
    existing_files: frozenset[str] | None = None,
) -> ValidationResult:
    """Run all 4 gates on an Action Items DAG.

    - `items`: list of Action Item dicts (see module docstring for shape).
      Each must have `id`, `touches`, `reads`, `depends_on`, `wave`.
    - `existing_files`: files already present in the codebase before this
      cycle. Used by gate 3 (reads satisfiable). Default empty set —
      every `reads` entry must then be produced by an earlier wave.

    Returns ValidationResult. The function NEVER raises for validation
    failures — it collects them into `result.errors`. It DOES raise
    ValueError for malformed inputs (missing required keys, wrong types).

    The `waves` field is the topological ordering as a tuple-of-tuples,
    each inner tuple being the ids in that wave. When gate 1 fails (cycle),
    waves is empty and CycleDetectedError is in errors.
    """
    # Fail-loud shape validation BEFORE any gate runs — malformed shapes
    # are agent bugs in the source proposal, not DAG-validation failures.
    _check_item_shape(items)

    if existing_files is None:
        existing_files = frozenset()

    errors: list[DAGValidationError] = []

    # Gate 1 — acyclic. If this fails, the other gates need a topological
    # order they cannot get, so we short-circuit to a cycle-only result.
    try:
        waves = topological_waves(items)
    except CycleDetectedError as err:
        return ValidationResult(ok=False, waves=(), errors=(err,))

    # Gate 4 — no orphan dependencies. Run before gates 2/3 so its errors
    # appear alongside any contention/read findings.
    known_ids = {item["id"] for item in items}
    for item in items:
        item_id = item["id"]
        for dep in item["depends_on"]:
            if dep not in known_ids:
                errors.append(
                    OrphanDependencyError(
                        f"Item {item_id} depends_on {dep!r} but no such item exists"
                    )
                )

    # Gate 2 — no within-wave file contention. For each wave, group items by
    # the files they touch; any file touched by 2+ items in the same wave is
    # a contention. Emit one error per (wave, file, pair).
    by_id: dict[str, dict] = {item["id"]: item for item in items}
    for wave_idx, wave in enumerate(waves, start=1):
        file_to_items: dict[str, list[str]] = {}
        for item_id in wave:
            # Defense-in-depth: even though _check_item_shape rejects intra-item
            # duplicates, ensure each (file, item_id) pair is counted at most once
            # so a future bypass of the shape check can never produce a false
            # "touched by both A and A" message.
            for filepath in by_id[item_id]["touches"]:
                owners = file_to_items.setdefault(filepath, [])
                if item_id not in owners:
                    owners.append(item_id)
        for filepath, owners in file_to_items.items():
            if len(owners) >= 2:
                # Pairwise report — covers the 2-owner common case and lists
                # all owners in larger pile-ups. Use the first two ids in the
                # message for readability; full list rendered afterwards when >2.
                if len(owners) == 2:
                    errors.append(
                        FileContentionError(
                            f"Wave {wave_idx}: {filepath!r} touched by both "
                            f"{owners[0]} and {owners[1]}"
                        )
                    )
                else:
                    errors.append(
                        FileContentionError(
                            f"Wave {wave_idx}: {filepath!r} touched by both "
                            f"{owners[0]} and {owners[1]} (and also: {owners[2:]})"
                        )
                    )

    # Gate 3 — reads satisfiable. Walk waves in order, accumulating files
    # produced by earlier waves. For each item in the current wave, every
    # file in `reads` must be in produced_so_far OR existing_files.
    produced_so_far: set[str] = set()
    for wave in waves:
        for item_id in wave:
            item = by_id[item_id]
            for filepath in item["reads"]:
                if filepath in produced_so_far:
                    continue
                if filepath in existing_files:
                    continue
                errors.append(
                    UnsatisfiableReadsError(
                        f"Item {item_id} reads {filepath!r} but no earlier wave "
                        f"produces it and it doesn't exist"
                    )
                )
        # Files touched by this wave become available to LATER waves only —
        # within-wave reads of a within-wave produced file are not allowed
        # (the producer hasn't run yet relative to a peer in the same wave).
        for item_id in wave:
            for filepath in by_id[item_id]["touches"]:
                produced_so_far.add(filepath)

    return ValidationResult(ok=not errors, waves=waves, errors=tuple(errors))
