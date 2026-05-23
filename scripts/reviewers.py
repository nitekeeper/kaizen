"""Reviewer selection for Phase 5b' — enforces disjointness from implementers.

Per internal/cycle/SKILL.md Phase 5b' the reviewers must not be the same
agents who implemented the work in Phase 4. This module is the single source
of truth for that rule.
"""

from __future__ import annotations


class InsufficientRosterError(ValueError):
    """Raised when the roster cannot supply enough disjoint reviewers."""


def select_reviewers(
    roster: list[str],
    implementers: list[str],
    n: int = 3,
    *,
    preferred_lenses: list[str] | None = None,
) -> list[str]:
    """Pick `n` reviewers from `roster`, disjoint from `implementers`.

    See module docstring for the disjointness rule. Pure function; deterministic
    given inputs. Lens preferences (when supplied) order matching candidates
    first in lens order, then non-matching candidates fill remaining slots
    in input order.
    A candidate matches a lens iff the lens string is a substring of the
    role id (case-sensitive); a role is counted at most once even if it
    matches multiple lenses, and earlier lenses take priority.
    """
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")

    # Deduplicate roster preserving first-occurrence order.
    seen: set[str] = set()
    deduped_roster: list[str] = []
    for role in roster:
        if role not in seen:
            seen.add(role)
            deduped_roster.append(role)

    implementer_set = set(implementers)
    pool = [role for role in deduped_roster if role not in implementer_set]

    if len(pool) < n:
        # Report which implementer ids actually overlapped the roster, in
        # first-occurrence order from the deduped roster — helps callers
        # spot typo'd implementer ids that weren't in the roster at all.
        overlap = [role for role in deduped_roster if role in implementer_set]
        raise InsufficientRosterError(
            f"only {len(pool)} disjoint candidates available (need {n}); "
            f"roster size {len(deduped_roster)}; "
            f"implementers overlapping roster: {overlap}"
        )

    if not preferred_lenses:
        return pool[:n]

    # Order candidates: lens-matching first (in lens order), then the rest in input order.
    ordered: list[str] = []
    used: set[str] = set()
    for lens in preferred_lenses:
        for role in pool:
            if role in used:
                continue
            if lens in role:
                ordered.append(role)
                used.add(role)
    for role in pool:
        if role not in used:
            ordered.append(role)
            used.add(role)

    return ordered[:n]
