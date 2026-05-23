# Kaizen — Design Spec

**Status:** design:open (drafted 2026-05-16)
**Atelier project ID:** 3
**Repo:** https://github.com/nitekeeper/kaizen.git (private)
**Location on disk:** sibling to `atelier/`, `memex/`, `agora/` under the user's `Skills/` directory

## 1. Purpose

Kaizen is a personal-use Claude Code plugin that runs Atelier's multi-agent improvement methodology against **any** git repository specified by URL. It generalizes `internal/self-improve/` (currently embedded in Atelier and hardwired to Atelier's own repo) into a standalone tool that targets external repositories via GitHub PRs.

After Kaizen ships, Atelier's `internal/self-improve/` skill and its scripts (`scripts/self_improve.py`, `scripts/destructive_check.py`, `tests/test_self_improve.py`) are removed; Kaizen replaces them.

## 2. Scope

### In scope

- Single user-invocable slash command: `kaizen:improve <git-url>` (with optional `--cycles N`, `--subject "..."`).
- Multi-cycle improvement runs against any target git repository.
- One bundled GitHub PR per run, containing all successful cycle commits and abandonment reports.
- Per-target project records (DB-stored config, auto-detected at first registration).
- Plugin-owned knowledge base (Memex) accumulating cross-repo improvement history.
- Vendoring of git/clone/destructive-check/migrate infrastructure from Atelier.

### Out of scope

- Distribution (Agora, PyPI, public release). Personal-use plugin.
- Cross-user knowledge sharing.
- Non-pytest test runners (initially — auto-detect proposes the right command on registration; user confirms).
- Continuous / scheduled execution (always user-initiated).
- Mid-PR review fix iteration (the PR's review/merge is the user's responsibility post-run).

## 3. User-facing surface

### 3.1 Slash command

```
kaizen:improve <git-url> [--cycles N] [--subject "<area to improve>"]
```

- `<git-url>` — required. https or ssh form (e.g., `https://github.com/owner/repo.git` or `git@github.com:owner/repo.git`).
- `--cycles N` — number of independent improvement cycles. Default: 1.
- `--subject` — optional focus area. If omitted, the PM agent decides per cycle.

### 3.2 Run lifecycle (happy path)

1. **Resolve target.** Look up `<git-url>` in Kaizen's `projects` table.
   - Found → load stored config.
   - Not found → register: clone, auto-detect (test command, language, read paths), confirm with user, save.
2. **Clone target** into `<kaizen>/experiment/<owner>-<repo>/`. Set up Atelier's full schema in `<clone>/.ai/memex.db` via `python3 <atelier>/scripts/migrate.py` + `seed_roles.py`. Ensure `<clone>/.ai/wiki/` exists.
3. **Create run branch** `kaizen/<subject-or-pm>-YYYY-MM-DD-HHMM` in the clone.
4. **For each cycle 1..N:**
   - PM agenda (Atelier's Phase 1)
   - Parallel pre-analysis (Atelier's Phase 2)
   - Synthesis meeting (Atelier's Phase 3)
   - Implementation in the clone (Atelier's Phase 4)
   - Quality gates: destructive check + tests (Atelier's Phase 5)
   - **If cycle succeeds:** commit on the run branch; record success in Kaizen's `cycles` table.
   - **If cycle is abandoned:** write formal abandonment report; capture to Kaizen's Memex via `memex capture`; record in `abandonments` table; **continue to next cycle** (skip-and-continue).
5. **Push run branch** to origin.
6. **Open one bundled PR** via `gh pr create` summarizing all cycles (successes + abandonments).
7. **Tear down clone** — `experiment/<owner>-<repo>/` deleted.
8. **Tell user** — print PR URL, cycle summary, kaizen memex slugs for follow-up reading.

### 3.3 Failure modes the user sees

- **Setup deps missing** — refuse with clear instructions (`gh`, `memex`, `git`, `atelier-via-agora`).
- **Target URL unreachable** — `git clone` error surfaced; project record not created.
- **All cycles abandoned** — PR still opened with abandonment-only body; no code commits. User can review the reports and decide whether to retry.
- **Push or PR creation fails** — clone preserved (not deleted) so the user can recover manually; surface git error.

## 4. Internal architecture

### 4.1 Directory layout

```
kaizen/
  .claude-plugin/
    plugin.json
  .ai/
    wiki/                   ← tracked, Memex source of truth
    memex.db                ← gitignored
  scripts/
    setup.py                ← verify deps + run migrate
    migrate.py              ← vendored from atelier
    db.py                   ← vendored from atelier
    git_utils.py            ← vendored from atelier
    platform_utils.py       ← vendored from atelier
    worktree.py             ← vendored from atelier (classify_status, helpers)
    destructive_check.py    ← vendored from atelier
    cycle.py                ← new: orchestrates one cycle inside the clone
    run.py                  ← new: top-level multi-cycle orchestration
    pr.py                   ← new: gh pr create wrapper
    project.py              ← new: register, get, list, edit projects
    detect_config.py        ← new: auto-detect test cmd, language, read paths
  migrations/
    001_kaizen_schema.sql   ← projects, runs, cycles, abandonments, migrations
  skills/
    improve/
      SKILL.md              ← only user-invocable slash command
  internal/
    run/SKILL.md
    cycle/SKILL.md
    project/SKILL.md
    run-record/SKILL.md
    pm-agenda/SKILL.md
    synthesis-meeting/SKILL.md
    abandonment-report/SKILL.md
    clone-target/SKILL.md
    expert-roster/SKILL.md
    open-pr/SKILL.md
  tests/
    test_*.py               ← pytest, mirrors atelier convention
  docs/
    design.md               ← this file
    plan.md                 ← next phase
  experiment/               ← gitignored, clones land here
  CLAUDE.md
  CHANGELOG.md
  README.md
  requirements.txt
```

### 4.2 DB schema (Kaizen's own `.ai/memex.db`)

Slim 5 tables; Atelier's full 14 tables are seeded into each cloned target separately.

```sql
CREATE TABLE projects (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  git_url           TEXT NOT NULL UNIQUE,
  name              TEXT NOT NULL,
  base_branch       TEXT NOT NULL DEFAULT 'main',
  test_command      TEXT NOT NULL,
  read_paths        TEXT NOT NULL,         -- JSON array
  expert_roster     TEXT NOT NULL,         -- JSON array of role ids
  language          TEXT,
  registered_at     TEXT NOT NULL,
  last_run_at       TEXT,
  notes             TEXT
);

CREATE TABLE runs (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id        INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  branch            TEXT NOT NULL,
  pr_url            TEXT,
  cycles_requested  INTEGER NOT NULL,
  cycles_succeeded  INTEGER NOT NULL DEFAULT 0,
  cycles_abandoned  INTEGER NOT NULL DEFAULT 0,
  subject           TEXT,
  started_at        TEXT NOT NULL,
  ended_at          TEXT,
  status            TEXT NOT NULL CHECK (status IN ('running', 'complete', 'failed'))
);

CREATE TABLE cycles (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id              INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  cycle_n             INTEGER NOT NULL,
  subject             TEXT,
  status              TEXT NOT NULL CHECK (status IN ('success', 'abandoned')),
  commit_sha          TEXT,
  minutes_memex_slug  TEXT,
  started_at          TEXT NOT NULL,
  ended_at            TEXT,
  UNIQUE (run_id, cycle_n)
);

CREATE TABLE abandonments (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle_id            INTEGER NOT NULL REFERENCES cycles(id) ON DELETE CASCADE,
  phase_reached       TEXT NOT NULL,       -- agenda | meeting | implementation | test | review | push
  reason              TEXT NOT NULL,       -- no_consensus | destructive_rejected | tests_unrecoverable | review_unrecoverable | other
  detail              TEXT NOT NULL,
  report_memex_slug   TEXT,
  created_at          TEXT NOT NULL
);

CREATE TABLE migrations (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  filename    TEXT NOT NULL UNIQUE,
  applied_at  TEXT NOT NULL
);
```

### 4.3 External dependencies

| Dep | Why | Failure mode |
|---|---|---|
| `git` | Clone, branch, commit, push | Setup script refuses |
| `gh` (authenticated) | Open PR | Setup script refuses |
| `memex` CLI on PATH | Capture / ask against Kaizen's own wiki | Setup script refuses |
| Atelier via Agora (`/atelier:*` slash commands + `scripts/seed_roles.py` callable on disk) | Provides 61-role roster + dev arc skills the cycle agents follow | Setup script refuses |
| Python 3.11+ + `pip install -r requirements.txt` | Run scripts | Standard pip error |

### 4.4 Cycle flow detail (per cycle)

A cycle borrows Atelier's `internal/self-improve/SKILL.md` Phase 1–5 structure, with these adaptations:

| Atelier phase | Kaizen equivalent | Note |
|---|---|---|
| 1. Agenda (PM) | Same. PM reads `read_paths` from project config. | |
| 2. Parallel pre-analysis | Same. Experts from `expert_roster` (standing 6 + domain picks). | |
| 3. Synthesis meeting | Same. Unanimous or DROPPED. | If all items dropped → cycle abandoned (`reason=no_consensus`). |
| 4. Implementation | Same. Agents edit files in the clone. | |
| 5a. Destructive check | Same. User approves or rejects per-item. | If all destructive rejected and nothing remains → cycle abandoned (`reason=destructive_rejected`). |
| 5b. Tests | Per project config (`test_command`). | If can't reach green within the cycle → cycle abandoned (`reason=tests_unrecoverable`). |
| 5c. Commit | Same. One commit per cycle, on the shared run branch. | |
| 5d. Push/merge | **Skipped per cycle** — done once at end of run. | |

### 4.5 Abandonment report format

Each abandoned cycle produces a markdown report captured to Kaizen's Memex via `memex capture`:

```markdown
---
id: kaizen:abandonment:<run-id>-cycle-<n>
title: Cycle <n> abandoned — <reason>
type: abandonment-report
project: <owner>-<repo>
status: draft
---

Cycle: <n>
Date: YYYY-MM-DD HH:MM UTC
Subject: <cycle subject or "PM-directed">
Participants: <agents>
Phase reached: <agenda | meeting | implementation | test | push>
Reason for abandonment: <no_consensus | destructive_rejected | tests_unrecoverable | push_failed | other>
Detail: <free-text — what was attempted, what blocked it, what the next session should reconsider>
Artifacts: <memex slugs of partial proposals, test logs, etc.>
```

The wiki slug (`kaizen:abandonment:<run-id>-cycle-<n>`) is stored in `abandonments.report_memex_slug` for later cross-referencing.

### 4.6 PR title and body template

```
title: kaizen: <subject or "PM-directed"> — N cycles, S succeeded / A abandoned

body:

## Summary

Multi-cycle improvement run against this repo.

| | |
|---|---|
| Cycles requested | N |
| Succeeded | S |
| Abandoned | A |
| Run started | YYYY-MM-DD HH:MM UTC |
| Run ended | YYYY-MM-DD HH:MM UTC |

## Cycles

### Cycle 1 — <status>
- Subject: <subject>
- Commit: <sha> (success) / —
- Minutes (Kaizen wiki): <slug>
- (abandoned cycles also list reason + detail summary)

…

## Abandonment reports

See Kaizen memex entries:
- `kaizen:abandonment:<run-id>-cycle-3`
- `kaizen:abandonment:<run-id>-cycle-5`

🤖 Generated by Kaizen against <target-repo> at <git-url>
```

## 5. Trade-offs and rejected alternatives

| Considered | Rejected because |
|---|---|
| Wait-for-merge between cycles | Sequential cycles compound state; slow. Bundled PR with skip-and-continue scales better for personal use. |
| Per-cycle PR (5 PRs for 5 cycles) | Review overhead; cycles are usually thematically related; one PR per `improve` invocation matches user intent. |
| Hardcoded per-repo config (atelier's pattern) | Doesn't generalize across repos. DB-stored config is small effort and supports cross-repo learning. |
| `.improve.yml` checked into target repo | Pollutes target; not all repos can or should carry plugin-specific files. DB-stored is invisible to the target. |
| Use rebuild-style DB (memex pattern) | Kaizen's data is authoritative, not derived. Migrations are the right model. |
| Out-of-band abandonment reports (filesystem outside repo) | Loses cross-repo searchability. Memex capture is the natural home. |
| Atelier-as-marketplace (atelier's own marketplace.json) | Resolved earlier by Agora migration. Kaizen sidesteps the issue by not distributing at all. |
| Sharing one DB file with Memex at `.ai/memex.db` | Memex's `rebuild.py` does `os.remove`; would clobber Kaizen's tables. Kaizen owns its own DB file; lives at `<kaizen>/.ai/memex.db` where Memex never operates. |

## 6. Open questions deferred to plan / implementation phase

1. PR title/body exact wording (above is a draft).
2. Auto-detect heuristics for `test_command` and `read_paths` per language (pytest is given; npm/cargo/go to be sketched in plan phase).
3. How agents inside Kaizen's cycle communicate "abandon now" to the orchestrator. (Likely a JSON marker in the cycle output, like Atelier's destructive check pattern.)
4. How the user edits stored project config after registration (a `kaizen:project edit` flow or just manual SQL — defer to plan).

## 7. References

- Atelier's `internal/self-improve/SKILL.md` — the canonical structure being generalized.
- Atelier's `scripts/self_improve.py` — vendored as `kaizen/scripts/self_improve.py` (renamed components as needed).
- Atelier's `scripts/seed_roles.py` — depended upon, not vendored.
- Atelier project record (id 3) — tracks this design's phase advancement.
