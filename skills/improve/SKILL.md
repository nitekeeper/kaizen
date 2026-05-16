---
description: Use when the user wants to run multi-agent improvement cycles against a git repository — runs N cycles and opens one bundled GitHub PR. Trigger on '/kaizen:improve <git-url>', 'run kaizen on <repo>', 'improve <repo>', or similar.
---

# improve

The only user-invocable Kaizen command. Runs N independent improvement cycles against the target git repository in a temporary clone, then opens one bundled GitHub PR summarising every successful and abandoned cycle.

This skill is intentionally thin: it parses the invocation, verifies hard dependencies, and routes through to `internal/run/SKILL.md` which does the actual orchestration. All the methodology lives in the internal procedures.

## Authority and override

User instructions override this skill's defaults at all times. If the user provides a direct instruction — "skip the destructive check," "don't open the PR," "abort after cycle 1" — comply immediately. Persistent instructions in CLAUDE.md or saved preferences pre-authorize routing choices without a live confirmation per session.

Priority order when instructions conflict:

1. **User's explicit instructions — highest priority.**
2. **Kaizen methodology (this skill + the internal procedures it routes to).**
3. **Default system prompt.**

## Invocation

```
/kaizen:improve <git-url> [--cycles N] [--subject "<area to improve>"]
```

- `<git-url>` — required. https or ssh (e.g., `https://github.com/owner/repo.git` or `git@github.com:owner/repo.git`).
- `--cycles N` — number of independent improvement cycles to run. Default: 1.
- `--subject "..."` — optional focus area. If omitted, the PM agent decides per cycle.

## Procedure

### Step 1 — Verify hard dependencies

Run from the Kaizen repo root:

```
python scripts/setup.py
```

This verifies `git`, `gh` (authenticated), `memex`, atelier on disk, and Python ≥ 3.11. If any check fails, the script prints actionable instructions and exits non-zero. **Abort** — do not proceed. Surface the script's output to the user verbatim and stop.

If `scripts/setup.py` has not previously run on this machine, it will also apply Kaizen's DB migrations. That is expected; the run is safe to proceed if all dependency checks pass.

### Step 2 — Parse arguments

Extract `git_url`, `cycles` (default 1), and `subject` (default None) from the user's invocation. Validate:

- `git_url` is present and looks like a git URL (https or ssh form).
- `cycles` is a positive integer.

If either check fails, ask the user to correct and stop until they do.

### Step 3 — Route to internal/run

Read `internal/run/SKILL.md` and follow its procedure inline, passing along `(git_url, cycles, subject)`. That procedure handles project lookup/registration, clone setup, cycle loop, push, PR open, and teardown.

### Step 4 — Print the final summary

When `internal/run/SKILL.md` returns, surface the summary to the user. It should include:

- `run_id` (Kaizen DB)
- PR URL (if the PR was opened)
- `S succeeded / A abandoned out of N requested`
- Memex slugs for any abandonment reports and cycle minutes, so the user can read them later via `memex ask`

## Hard rules

- **User-initiated only.** No agent may invoke `/kaizen:improve` from within another skill, script, or autonomous flow. Kaizen is always run by a human.
- **Setup must pass before any clone or PR action.** If `scripts/setup.py` exits non-zero, abort without touching the target repo.
- **One PR per invocation.** All N cycles in a single `/kaizen:improve` run produce exactly one bundled PR. Cycle-per-PR is out of scope.
- **The clone is the only work area.** Kaizen never writes to the user's local copy of the target repo. The work happens in `<kaizen-root>/experiment/<owner>-<repo>/` and the directory is deleted after the PR opens.
- **Abandonment of one cycle does not stop the run.** A cycle that cannot complete writes a formal report (captured to Kaizen's own memex) and the next cycle still runs. See `internal/run/SKILL.md` for the skip-and-continue policy.
