---
title: "[low] Add Bandit + pip-audit to in-clone CI mirror"
labels: enhancement
---

## Context

`scripts/ci_runner.run_ci_checks()` mirrors target-repo CI locally so Phase 5b agents can verify before opening a PR. Currently it runs `pytest` + `ruff check` + `ruff format --check`. It does NOT run Bandit or pip-audit, both of which are part of `atelier`'s GitHub Actions surface. As a result, implementer prompts can't catch security-scan failures locally — run 23 / PR#34 needed a follow-up recovery commit (`977c76b`) to suppress a Bandit B608 false positive that should have been caught in the clone.

Effort note: classified M (not S). Implementation requires multiple detection paths, distinguishing Bandit's non-zero-on-findings exit from genuine crashes, and updating Phase 5b routing. pip-audit also requires network access, so consider an opt-out for offline runs.

## Where

- `scripts/ci_runner.py` — `run_ci_checks()` needs new check handlers (current implementation handles `tests`, `ruff_check`, `ruff_format`, and a `lint_warning` fallback)

## Suggested approach

- Mirror the ruff opt-in pattern (`_has_ruff_config()`):
  - Detect Bandit via `pyproject.toml [tool.bandit]`, `.bandit`, or `bandit.yaml`
  - Detect pip-audit via target's `.github/workflows/*.yml` referencing `pip-audit`
- Add `bandit` and `pip_audit` result keys when detected
- Skip silently with `lint_warning`-style stubs when not configured
- Distinguish Bandit non-zero-on-findings (treat as a failed check) from FileNotFoundError / non-zero-on-crash (surface as infra failure)
- Add a `KAIZEN_SKIP_PIP_AUDIT=1` opt-out for offline runs (pip-audit hits PyPI)
- Update `internal/cycle/SKILL.md` Phase 5b routing rules to consume the new keys
- Add tests covering: both tools detected, only one detected, neither detected, Bandit-findings-vs-crash distinction

## Acceptance criteria

- [ ] `bandit` and `pip_audit` checks run when their opt-in configs are present
- [ ] Both checks skip cleanly (logged warning, not failure) when not configured
- [ ] Bandit exit-code semantics: findings produce a failed result with output; missing binary or crash surfaces a distinct error message
- [ ] Offline opt-out for pip-audit honored
- [ ] Phase 5b routing rules in `internal/cycle/SKILL.md` updated to handle the new check names
- [ ] `tests/test_ci_runner.py` covers all four detection combinations plus the Bandit findings-vs-crash distinction
- [ ] Module docstring lists the full set of supported checks

## Related

- Origin: run 23 / PR#34 Bandit B608 recovery (`977c76b`)
- Context doc: `docs/planning/deferred-todos.md` item 5
