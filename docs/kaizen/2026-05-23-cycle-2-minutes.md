# Cycle 2 Minutes — Run 16

- **Date:** 2026-05-23
- **Subject:** Production TeamTools wrapper + 10 SendMessage dispatch templates (extracted from team_executor inline briefs)
- **Participants:** Akira Sato (backend-engineer-1), Sara Lindqvist (prompt-engineer-1, independent reviewer)
- **Status:** success
- **Fix-loop iterations:** 2 (initial extraction had wire-drift on phase_5b_ci_failure + _require accepted empty containers; iter 2 fixed both)

## Context

Cycle 1 shipped the real Phase 1-5c orchestration with 8+ inline `_phase_*_brief` functions. This cycle extracts them into a formal dispatch-templates module + ships the production wrapper the orchestrating agent uses to wire real CC tool calls.

## Decisions

1. **`scripts/dispatch_templates.py`** — 10 named templates, keyword-only, validated via `_require` (rejects missing, wrong-type, AND empty containers; numeric exempt). Each template is a pure function with explicit kwarg validation; failure is LOUD and LOCAL.
2. **`scripts/team_tools_wrapper.py`** — `AgentTeamsWrapper` base class with default methods that `raise NotImplementedInThisRuntime` (the orchestrating agent subclasses to wrap real CC tool calls); `RecordingWrapper` for harness tests. Both satisfy the `TeamTools` Protocol.
3. **Wire protocol BYTE-IDENTITY preserved** — 9 of 10 templates emit text byte-identical to cycle 1's inline `_phase_*_brief`. The 10th (`phase_5b_ci_failure`) was new (extracted from inlined detail string); reviewer iteration caught initial drift and restored byte-identity to cycle 1's `f"CI failed after wave {wave_n}: {failed_checks}"`. Literal-pin test locks it.
4. **`internal/cycle/SKILL.md`** — `mode='team'` branch now references both `AgentTeamsWrapper` (the subclass contract) and `scripts.dispatch_templates` (the message contract). Tests use `RecordingWrapper`.

## Implementation

- **New:** `scripts/dispatch_templates.py` (10 templates + `_require` validator)
- **New:** `scripts/team_tools_wrapper.py` (`NotImplementedInThisRuntime`, `AgentTeamsWrapper`, `RecordingWrapper`)
- **Modified:** `scripts/team_executor.py` (deleted 8 inline brief functions + inlined CI-failure detail string; replaced with 10 template imports + call-site swaps)
- **Modified:** `internal/cycle/SKILL.md` (callout for wrapper + dispatch_templates)
- **New tests:** `tests/test_dispatch_templates.py` (15 tests including literal-pin for CI failure + empty-container rejection across all required-container slots), `tests/test_team_tools_wrapper.py` (6 tests)

## Fix-loop iteration history

**Iteration 1:** 14 dispatch_templates tests + 6 wrapper tests + 8 inline functions deleted + executor wired to new templates. Test count 339 → 359.

**Independent review (Sara Lindqvist, prompt-engineer):** verified all 7 cycle-1 invariants intact + all 4 cycle-1 majors preserved + all 42 cycle-1 tests still pass + 9/10 templates byte-identical. Found 2 BLOCKERS:
- B1: `phase_5b_ci_failure` text drifted (added `failed checks=` prefix and `Details: {results}` suffix). Test only checked substrings, would have shipped silently.
- B2: `_require` accepted empty containers (`_require("agenda_items", [], list)` PASSED). Silent semantic failure — agent would receive a Phase-2 brief asking it to address no items.

Plus 2 majors folded into the blocker fixes (literal-pin test for CI failure + `={value!r}` in wrong-type message).

**Iteration 2:** all fixed. Wire text reverted to byte-identical cycle-1 string. `_require` now rejects empty list/dict/str; numeric types (`iter_n=0`) deliberately exempt. 5 new empty-rejection tests + 1 literal-pin replacement. Test count 359 → 365.

**Re-review:** every blocker + major VERIFIED. Cycle-1 invariants + Major fixes RE-AFFIRMED intact. Verdict: **READY TO COMMIT**.

## What this unlocks

Team-mode is now END-TO-END usable:
- Substrate (cycle 4 of PR#23): lifecycle skeleton with team_delete-in-finally, runtime Protocol preflight, exact outcome shapes
- Real orchestration (cycle 1 of this run): 6-phase flow using dag.py + fix_loop.py + reviewers.py + ci_runner.py
- Dispatch templates + production wrapper (this cycle): the orchestrating agent subclasses `AgentTeamsWrapper`, overrides 3 methods to call real CC tools, and the executor handles everything else

A `kaizen:improve --mode team` invocation can now run a real cycle against a real target. The orchestrating agent's only remaining job is to write the production `AgentTeamsWrapper` subclass — a small Python class wrapping `TeamCreate` / `SendMessage` / `TeamDelete` from its own tool context.

## Residual minor (deferred — not blocking)

- `phase_5b_ci_failure`'s `results` kwarg is validated but excluded from the returned string (kept as optional logging input). If no caller needs it, could be removed in a future cleanup cycle.
- `_require` doesn't validate empty containers for `tuple` or `set` (only `list`/`dict`/`str`). Reasonable for the current template signatures; revisit if a template ever takes one.
- The wire protocol byte-identity invariant is implicit in tests; a single "all templates byte-identical to cycle-1" CI guard test would be belt-and-braces.
