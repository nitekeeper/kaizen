# Cycle 2 Minutes — Run 14

- **Date:** 2026-05-23
- **Subject:** Phase 5b' substrate — `scripts/abandonment.py` structured fields + migration 004 (`phase_reached='review'`)
- **Participants:** Akira Sato (backend-engineer-1, implementer), Diane Park (sdet-1, independent reviewer; migrations specialist)
- **Status:** success
- **Fix-loop iterations:** 2 (one round of blocker fixes, one round of verification)

## Context

Sibling to Cycle 1. The 3-agent audit on 2026-05-23 (Holbrook, Lindqvist, Park) identified a **category-error bug** + **schema gap** + **structured-fields gap** in the Phase 5b' abandonment path:

1. `internal/cycle/SKILL.md:258` used `phase_reached: "test"` for review-loop exhaustion (wrong category — should be `"review"`).
2. `migrations/003_review_unrecoverable.sql:13` CHECK omitted both `'review'` (the value needed) and `'push'` (already documented in abandonment-report SKILL but never schema-permitted).
3. The four structured fields a review-loop abandonment needs (iteration count, unresolved findings, convergence summary, reviewer attribution) had no DB columns and no Python plumbing.

## Decisions

1. **`migrations/004_phase_reached_review.sql`** recreates `abandonments` with `phase_reached IN ('agenda','meeting','implementation','test','review','push')` and adds 4 nullable columns: `review_iteration_count` (INT), `unresolved_findings` (TEXT/JSON), `convergence_summary` (TEXT), `reviewer_attribution` (TEXT/JSON). Migration uses explicit column-list `INSERT...SELECT` (strictly better than 003's `INSERT...SELECT *`); index `idx_abandonments_cycle_id` recreated.
2. **`scripts/abandonment.py`** extended additively — `record_abandonment` / `process_abandonment` / `format_report` accept the 4 new kwargs as keyword-only with `None` defaults; JSON serialise on write, deserialise on read; markdown gains a "Review-loop details (Phase 5b' only)" section when any field is populated.
3. **`scripts/db.py::row_to_dict_with_json`** + `ABANDONMENT_JSON_COLUMNS` constant hoisted as the single source of truth for JSON-column row decoding. Both `scripts/abandonment.py:207` and `scripts/pr.py:80` consume it — no more asymmetric round-trip.
4. **Category-error bug fixed:** `internal/cycle/SKILL.md` line 272 (Phase 5b' abandonment shape) now uses `phase_reached: "review"`. The unchanged `phase_reached: "test"` on line 214 (Phase 5b `tests_unrecoverable`) is correct.
5. **Caller threading:** `scripts/run.py::orchestrate_run` now passes all 4 review-loop fields from the cycle outcome dict into `process_abandonment` (Blocker 1 fix). Without this, Phase 5b' abandonments would have silently dropped the data.
6. **Documentation alignment:** `internal/run/SKILL.md` Step 6.3 lists the 4 new params; `internal/abandonment-report/SKILL.md` documents `review` as a valid `phase_reached` value plus the structured-fields shape; `scripts/team_executor.py` outcome-contract docstring lists all 6 phases + 5 reasons + 4 optional review fields.

## Implementation

- **New:** `migrations/004_phase_reached_review.sql`
- **Modified:** `scripts/db.py` (+35 LOC — shared helper), `scripts/abandonment.py` (+133 LOC — kwargs + JSON round-trip + render), `scripts/pr.py` (+8 LOC — switch to shared helper), `scripts/run.py` (+8 LOC — thread kwargs), `scripts/team_executor.py` (+10 LOC — enum drift fix), `internal/cycle/SKILL.md` (Phase 5b' shape + valid-values list), `internal/abandonment-report/SKILL.md` (params + new subsection), `internal/run/SKILL.md` (param list extension)
- **Tests:** `tests/test_abandonment.py` (+246 LOC — 8 new tests including snapshot pin + lenient-findings), `tests/test_migrate.py` (+43 LOC — post-004 constraint + valid-values combinatorics 20→30), `tests/test_pr.py` (+59 LOC — JSON-decode round-trip regression guard), `tests/test_setup.py` (idempotency count 3→4)

## Fix-loop iteration history

**Iteration 1 (implementer self-review):** all 6 declared deliverables done, 268 tests passing, ruff clean. Reported READY.

**Independent review (Diane Park, SDET):** found **3 BLOCKERS** the implementer missed:
- Blocker 1: `scripts/run.py:279-291` `process_abandonment(...)` called without the 4 new kwargs — silent data loss for every future Phase 5b' abandonment.
- Blocker 2: `internal/run/SKILL.md:94` documented param list omitted the 4 new fields — agent-driven runs would also drop data.
- Blocker 3: `scripts/pr.py::load_run_context` used its own `_row_to_dict` that didn't deserialise JSON columns — `ab["unresolved_findings"]` would come back as a TEXT string, contradicting the abandonment.py contract.

Plus 4 majors (missing pr.py regression test; no snapshot test for format_report rendering; no missing-keys finding test; team_executor docstring enum drift).

**Iteration 2 (implementer fix patch):** all 3 blockers and all 4 majors addressed; JSON-decoding helper hoisted into `scripts/db.py` as the architectural fix. Test count 268 → 271 (net +3 net-new tests). Ruff clean.

**Re-review (Diane Park):** every blocker and major VERIFIED. Deferred pre-existing items confirmed untouched. Verdict: **READY TO COMMIT**.

## Deferred — pre-existing footguns (separate cycle warranted)

These predate cycle 2 and were explicitly out of scope:

1. `docs/design.md:173` — SQL comment lists old 5-value phase_reached enum, missing `'review'`. Documentation drift.
2. `internal/abandonment-report/SKILL.md:170` — recommends `phase_reached="unknown"` as the safe default. The CHECK rejects this value. Latent CHECK-violation bug if anyone follows the recommendation.
3. `scripts/run.py:287` — defaults `phase_reached=outcome.get("phase_reached", "unknown")`. Same latent CHECK-violation if a cycle outcome is malformed.

Recommend a follow-up cycle targeted at these three (small, mechanical, but worth doing together since they share a root cause: an "unknown" sentinel was assumed but never schema-allowed).

## What this unlocks

The Phase 5b' fix loop (max 5 iterations per cycle, per `internal/cycle/SKILL.md:249`) can now abandon with a faithful record: iteration count, unresolved findings, reviewer attribution, and convergence summary all persist into `abandonments` and into the rendered markdown. Cross-run analysis via `memex ask` can ask "which Phase 5b' fix loops exhausted and why?" — previously the answer was "all we know is the cycle abandoned for tests_unrecoverable", which was both wrong (it was review, not tests) and lossy (no structured data).
