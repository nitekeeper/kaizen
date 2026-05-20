# Kaizen — Implementation Plan

**Status:** plan:open (drafted 2026-05-16)
**Spec:** [docs/design.md](design.md)
**Atelier project ID:** 3

This plan breaks the design into ordered, independently shippable waves. Each wave ends with a passing test suite and a commit. Per established workflow preference: parallel within a wave where independent, sequential between waves.

## Wave 0 — Repo bootstrap (mostly done)

Already complete:
- [x] Folder + git init + private GitHub remote
- [x] README + .gitignore (editor folders included)
- [x] Atelier project record (id=3, phase tracking)
- [x] docs/design.md

Remaining:
- [ ] `CLAUDE.md` — methodology pointer + setup instructions
- [ ] `requirements.txt` — runtime deps
- [ ] `.claude-plugin/plugin.json` — plugin manifest (name=kaizen, version 0.1.0, description, author)
- [ ] `CHANGELOG.md` — empty seed

Commit: `chore: scaffold kaizen plugin metadata`

## Wave 1 — DB foundation

Goal: kaizen's own DB exists and is migration-managed.

Vendor from atelier (verbatim, no logic changes):
- `scripts/db.py`
- `scripts/migrate.py`
- `scripts/platform_utils.py`
- `scripts/git_utils.py`

New:
- `migrations/001_kaizen_schema.sql` — DDL from design §4.2 (projects, runs, cycles, abandonments, migrations)

Tests:
- `tests/test_migrate.py` — schema applies cleanly, all 5 tables present, FK constraints enforced
- `tests/test_db.py` — WAL mode + FK pragma on connection

Commit: `feat: kaizen DB schema + migration runner`

## Wave 2 — Project CRUD + auto-detect

Goal: `python3 scripts/project.py register <git-url>` works end-to-end (clone, detect, confirm via stdout/stdin, write row).

New:
- `scripts/project.py` — CRUD CLI (register, get, list, edit, delete)
- `scripts/detect_config.py` — language detection + test command + read paths inference per language

Detection heuristics (initial):
| Signal | Detected language | test_command default | read_paths default |
|---|---|---|---|
| `pyproject.toml` or `pytest.ini` or `setup.py` | python | `pytest -v --tb=short` | `["scripts/*.py", "tests/*.py", "skills/*/SKILL.md", "CLAUDE.md", "README.md"]` |
| `package.json` + scripts.test | javascript | from `package.json:scripts.test` | `["src/**/*.{js,ts}", "test/**/*", "README.md"]` |
| `Cargo.toml` | rust | `cargo test` | `["src/**/*.rs", "tests/**/*", "Cargo.toml", "README.md"]` |
| `go.mod` | go | `go test ./...` | `["**/*.go", "go.mod", "README.md"]` |
| _none of the above_ | unknown | _prompt user_ | _prompt user_ |

Expert roster default (per language):
- All: standing 6 (Agent Systems Architect, AI Safety Researcher, Prompt Engineer, AI Ethicist, AI Research Scientist, Cognitive Scientist)
- + python: backend-engineer-1, data-engineer-1
- + javascript: frontend-engineer-1, fullstack-engineer-1
- + rust: systems-architect-1, backend-engineer-1
- + go: backend-engineer-1, software-architect-1

Tests:
- `tests/test_detect_config.py` — fixture repos per language; assert detection output
- `tests/test_project.py` — register/get/list/edit/delete roundtrip; unique git_url enforced; first-registration confirmation flow (mocked stdin)

Commit: `feat: project registration with auto-detect`

## Wave 3 — Vendored cycle infrastructure

Goal: clone + destructive-check + test-runner all callable from kaizen's own scripts.

Vendor from atelier (light adaptation as noted):
- `scripts/self_improve.py` → split into:
  - `scripts/clone.py` — clone_repo, cleanup_experiment, get_remote_url (target-URL based, not origin)
  - `scripts/cycle_git.py` — create_branch (using kaizen naming `kaizen/<subject-or-pm>-YYYY-MM-DD-HHMM`), commit_cycle, push_branch
  - `scripts/test_runner.py` — run_tests_in_clone (parameterized by `test_command` from project config)
- `scripts/destructive_check.py` — verbatim
- `scripts/worktree.py` — vendor (we don't operate on worktrees, but `classify_status` may be reused; safe to ship)

New:
- `scripts/seed_atelier_in_clone.py` — wraps the subprocess invocations of atelier's `migrate.py` + `seed_roles.py` against `<clone>/.ai/memex.db`. Also ensures `<clone>/.ai/wiki/` exists.

Tests:
- `tests/test_clone.py` — clone from bare remote + cleanup
- `tests/test_cycle_git.py` — branch naming format, commit message format
- `tests/test_test_runner.py` — parameterized test command (pytest vs. mock npm)
- `tests/test_destructive_check.py` — copy of atelier's tests, verify vendored copy works
- `tests/test_seed_atelier_in_clone.py` — atelier schema + roles seeded in the clone's DB

Commit: `feat: vendor clone, destructive-check, test-runner from atelier`

## Wave 4 — Run + cycle orchestration

Goal: `python3 scripts/run.py <project_id> --cycles N --subject "..."` executes the full multi-cycle flow against the registered project's clone, with abandonment handling and run-record tracking.

New:
- `scripts/run.py` — top-level orchestrator: load project, clone, seed atelier, create run row + branch, loop cycles, write run summary, cleanup. Returns enough state for PR-open step.
- `scripts/cycle.py` — single-cycle executor that the orchestrator calls N times. Returns cycle outcome (success / abandoned + reason / abandoned + detail).
- `scripts/abandonment.py` — write abandonment report markdown, invoke `memex capture` against kaizen's own wiki, return memex slug.

Important: `scripts/cycle.py` does NOT itself perform the multi-agent meeting — that's done by the agent reading `internal/cycle/SKILL.md`. The script provides infrastructure (DB row inserts, file path resolution, git commit) that the agent's SKILL procedure calls.

Tests:
- `tests/test_run.py` — orchestrator integration test with mocked cycles (force success / abandonment scenarios); verifies run-record row + cycles rows + cleanup
- `tests/test_cycle.py` — single-cycle DB writes
- `tests/test_abandonment.py` — report markdown format + memex capture invocation (mock CLI)

Commit: `feat: run + cycle orchestration with abandonment handling`

## Wave 5 — PR open

Goal: `python3 scripts/pr.py <run_id>` opens the bundled PR via `gh pr create`.

New:
- `scripts/pr.py` — read run + cycles + abandonments from DB, render PR body from template (design §4.6), invoke `gh pr create`, write returned URL back to `runs.pr_url`.

Tests:
- `tests/test_pr.py` — body rendering (golden file), `gh` invocation (mocked); error handling when gh is not authenticated

Commit: `feat: bundled PR open via gh CLI`

## Wave 6 — Setup script + dependency verification

Goal: `python3 scripts/setup.py` verifies all external deps and runs the migration.

New:
- `scripts/setup.py` — checks `git --version`, `gh auth status`, `memex --version`, atelier slash commands available (via filesystem check at the agora install path or a documented env var). Runs `migrate.py` against `.ai/memex.db`. Exit non-zero with actionable messages on failure.

Tests:
- `tests/test_setup.py` — verification logic with mocked subprocess responses; migrate invocation; idempotent re-run

Commit: `feat: setup script with dependency verification`

## Wave 7 — Skills (the prose layer)

Goal: all 11 SKILL.md files written. This is where the methodology lives — Python scripts are the infrastructure, SKILL.md is the procedure.

Files (each is a separate SKILL.md):
- `skills/improve/SKILL.md` — the only user-invocable command. Description triggers on "kaizen", "improve", "improvement cycle" + the explicit `/kaizen:improve` invocation. Body: parse args, dispatch to `internal/run/`.
- `internal/run/SKILL.md` — N-cycle orchestration procedure. Calls `internal/project/` to register or load; calls `internal/clone-target/`; loops `internal/cycle/` N times; calls `internal/open-pr/`; teardown.
- `internal/cycle/SKILL.md` — single cycle. Mirrors atelier `internal/self-improve/` Phase 1–5 with kaizen adaptations (no push/merge, abandonment writes via `internal/abandonment-report/`).
- `internal/project/SKILL.md` — register/get/list/edit; first-registration prompt-and-confirm flow.
- `internal/run-record/SKILL.md` — create / update / get / list run rows.
- `internal/pm-agenda/SKILL.md` — Phase 1 procedure, parameterized by `read_paths` from project config.
- `internal/synthesis-meeting/SKILL.md` — Phase 3 procedure; unanimous-or-drop.
- `internal/abandonment-report/SKILL.md` — write the markdown report (design §4.5 format), capture to kaizen memex, record `abandonments` row with returned slug.
- `internal/clone-target/SKILL.md` — clone target URL + seed atelier schema + ensure `.ai/wiki/` exists; teardown after run.
- `internal/expert-roster/SKILL.md` — read project's `expert_roster` from DB; merge with standing 6; resolve role ids → agent profiles via atelier seed.
- `internal/open-pr/SKILL.md` — render PR body from template (design §4.6) and invoke `gh pr create`.

Tests:
- `tests/test_skill_frontmatter.py` — every SKILL.md has valid YAML frontmatter with required `description` field (mirrors atelier's pattern)
- `tests/test_session_open_hook.py` (skip for now — kaizen doesn't have session-open behavior)

Commit: `feat: write all skill procedures`

## Wave 8 — Plugin metadata + first end-to-end run

Goal: kaizen loads as a Claude Code plugin (locally; no Agora) and the first real `kaizen:improve` invocation succeeds against a trivial test target.

- `.claude-plugin/plugin.json` finalized
- Register kaizen as a local plugin in `~/.claude/settings.json` (or equivalent — document the exact step in CLAUDE.md)
- Smoke-test against a tiny throwaway repo (create one specifically for this — `nitekeeper/kaizen-smoke-test` with 1 file and 1 trivial test)
- Verify: registration prompts work, cycle runs, PR opens, clone deleted, run-record + cycles rows present, memex entries searchable

If smoke test passes:

Commit: `chore: first end-to-end smoke test passed`

## Wave 9 — Atelier cleanup

Goal: remove self-improve from atelier now that kaizen replaces it.

Inside atelier:
- Delete `internal/self-improve/SKILL.md`
- Delete `scripts/self_improve.py`
- Delete `scripts/destructive_check.py`
- Delete `tests/test_self_improve.py`
- Update `CLAUDE.md` — remove self-improve references, add "improvement is handled by kaizen" pointer
- Update `CHANGELOG.md` — note removal
- Bump atelier plugin version

Run atelier's tests; should pass (the removed pieces are atelier-internal; nothing else depends on them).

Commit (in atelier): `feat: remove self-improve; superseded by kaizen plugin`
Re-register via `agora:plugin-register --url https://github.com/nitekeeper/atelier.git`

## Wave 10 — Documentation polish

- README: usage examples, how to install kaizen locally, what to do when something goes wrong
- CLAUDE.md: agent-facing methodology pointer
- CHANGELOG: 0.1.0 release notes

Commit: `docs: 0.1.0 release notes`

Tag release: `git tag v0.1.0 && git push --tags`

## Dependency graph (which waves block which)

```
Wave 0 (scaffold) ──┐
                    ├─→ Wave 1 (DB) ──→ Wave 2 (project) ──┐
                    │                                       │
                    │                                       ├─→ Wave 4 (run/cycle) ──→ Wave 5 (PR) ──┐
                    └─→ Wave 3 (vendored infra) ────────────┘                                          ├─→ Wave 7 (skills) ─→ Wave 8 (e2e) ─→ Wave 9 (atelier cleanup) ─→ Wave 10 (docs)
                                                                                                      │
                                                                                Wave 6 (setup) ───────┘
```

Waves that can run in parallel:
- **Wave 1 + Wave 3** (DB foundation and vendored infrastructure are independent)
- **Wave 5 + Wave 6** (PR script and setup script are independent)
- Within each wave, the SKILL.md files in Wave 7 can be drafted in parallel

## Out-of-scope deferrals (named, not solved)

- **Wave 11+ (future)**: edit-config UI for stored projects (currently only the register flow is interactive; edits require manual SQL or a future command).
- **Wave 11+ (future)**: abandonment heuristics — pattern recognition across runs ("this kind of cycle keeps abandoning for the same reason — propose a different angle").
- **Future**: support for non-pytest test runners with their own auto-detection (npm/cargo/go are sketched in Wave 2 but not implemented end-to-end until a non-Python target is on the agenda).
- **Future**: Agora distribution. Requires deciding on plugin namespacing, generalizing setup, and writing user-facing docs for non-author users.

## Test discipline (applies to every wave)

- Red → green → clean within each wave. Tests precede implementation where possible.
- Each wave's commit must leave the suite passing.
- Use atelier's existing test fixtures patterns (bare_remote, source_repo) where applicable — they're solid.

## Estimated scope

| Wave | Lines (rough) | Risk |
|---|---|---|
| 0 | ~100 (mostly metadata) | low |
| 1 | ~200 (DB + 1 migration + tests) | low |
| 2 | ~400 (CRUD + detection + tests) | medium — detection heuristics may need tuning |
| 3 | ~500 (vendor + adaptations + tests) | low — mostly copy-paste with small renames |
| 4 | ~600 (run/cycle orchestration + tests) | high — the integration-heavy piece |
| 5 | ~200 (PR body + gh invoke + tests) | low |
| 6 | ~200 (setup verification + tests) | low |
| 7 | ~1500 (11 SKILL.md files) | medium — mostly prose; needs cross-skill consistency |
| 8 | ~50 (config + smoke fixes) | medium — first real run will surface issues |
| 9 | ~50 (atelier removal) | low |
| 10 | ~200 (docs polish) | low |

Total: ~4000 lines, roughly 60% Python + 40% Markdown.
