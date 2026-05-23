# Cycle 1 Minutes — Run 15

- **Date:** 2026-05-23
- **Subject:** Close the "unknown" sentinel cluster — 3 deferred items from PR#22 review
- **Participants:** Akira Sato (backend-engineer-1, implementer), Vivienne Holt (sdet-1, independent reviewer; latent-bug archaeology)
- **Status:** success
- **Fix-loop iterations:** 2 (initial implementation caught only `is None`; review iteration tightened to allowlist)

## Context

PR#22's review (Diane Park) flagged 3 pre-existing latent bugs sharing one root cause: the literal string `"unknown"` was used as a `phase_reached` sentinel but the CHECK constraint in `migrations/004` rejects it. No production run had hit the bug yet, but a single malformed cycle outcome would have raised `sqlite3.IntegrityError` at `INSERT INTO abandonments` time, crashing the run AFTER its work was done.

## Decisions

1. **`scripts/abandonment.py`** is the single source of truth for the enum values. New module-level frozensets `VALID_PHASES` and `VALID_REASONS` co-locate the Python contract with the SQL CHECK in `migrations/004`. Comment cross-references the migration in both directions.
2. **`scripts/run.py::orchestrate_run`** is the enforcement layer. The cycle-loop abandonment branch now allowlist-checks `phase_reached` AND `reason` against the imported frozensets BEFORE calling `process_abandonment`. Any invalid value (including `None`, `"unknown"`, or any typo like `"review_unrecoverable_"`) raises `ValueError` fail-loud with a message naming the cycle, the rejected value, and the full canonical menu.
3. **No DB-layer fallback.** `internal/abandonment-report/SKILL.md:170` hard rule was rewritten from a vague "raise `ValueError`" to an imperative naming the responsible layer (`scripts/run.py::orchestrate_run`) and forbidding fallback implementations inside `process_abandonment` / `record_abandonment` — by the time control reaches the DB layer, the CHECK has already fired and the cycle's work is lost.
4. **The `outcome.get("reason", "other")` default at the old call site is removed.** `"other"` is schema-valid but the implicit default hid the symmetric `reason` typo bug. `reason` is now bound to the validated value.
5. **`docs/design.md:173-174`** schema comment drift fixed — both `phase_reached` and `reason` comments now match `migrations/004` exactly.

## Implementation

- **Modified:** `scripts/abandonment.py` (+13 LOC — frozensets), `scripts/run.py` (+18 LOC — allowlist guards, drop default), `docs/design.md` (2-line comment fix), `internal/abandonment-report/SKILL.md` (hard rule rewrite)
- **Tests:** `tests/test_abandonment.py` (+1 test: CHECK-rejects-unknown pinned to exact `"CHECK constraint failed"` string), `tests/test_run.py` (+4 tests: `*_phase_reached_missing` + `*_is_unknown` + `*_is_bogus` + `*_reason_is_invalid`; parametrize-6-phases acceptance test from iteration 1; new helpers `_assert_all_phases_in_message` / `_assert_all_reasons_in_message` enforce strict message contract)

## Fix-loop iteration history

**Iteration 1 (implementer self-review):** 5 deliverables done. Implementer chose `is None` as the guard scope. Test count 271 → 279.

**Independent review (Vivienne Holt, SDET):** found 2 BLOCKERS:
- Blocker 1: `is None` only catches missing-key; an executor that emits `phase_reached="unknown"` or `"bogus"` still crashes at INSERT — the exact original bug surface.
- Blocker 2: enum sets not co-located with the SQL contract — drift will return.

Plus 4 majors (3 missing test cases for explicit-invalid; loose CHECK substring match; vague SKILL.md; missing all-values-in-message assertion).

**Iteration 2 (implementer fix patch):** all 2 blockers + all 4 majors fixed. Allowlist-based guards on both `phase_reached` AND `reason`. Frozensets hoisted into `scripts/abandonment.py`. Test count 279 → 282 (+3 explicit-invalid tests; existing missing-key test strengthened).

**Re-review (Vivienne Holt):** every blocker + major VERIFIED. Spot-checks confirmed `is None` was REPLACED not augmented; reason default truly removed everywhere; frozensets immutable; SKILL.md is imperative not advisory; new tests catch ValueError specifically (not IntegrityError). Verdict: **READY TO COMMIT**.

## What this unlocks

Every value `scripts/run.py` can possibly emit into the `abandonments` row is now provably schema-accepted. The bug class "sentinel string getting into a CHECK-constrained INSERT" cannot return without a deliberate code change that would also trip the new tests. Migration 005 (if it ever extends the enum) will need to update the frozenset in one place — no more 3-file drift.
