# CLAUDE.md — Kaizen

Kaizen is a personal-use Claude Code plugin. It runs Atelier's multi-agent improvement methodology against any git repository specified by URL — clone → N improvement cycles → bundled GitHub PR.

## Hard dependencies

Kaizen refuses to run if any of these are missing:

- `git` on PATH
- `gh` CLI on PATH and authenticated (`gh auth status` exits 0)
- Atelier installed via Agora (`atelier:run` skill available) — Kaizen depends on Atelier's 61-role roster + dev-arc skills for the cycle agents; accessed via the plugin, not a CLI
- Memex installed via Agora (`memex:run` skill available) — used by agents to capture abandonment reports and cycle minutes; accessed via the plugin, not a CLI
- Python 3.11+ with `pip install -r requirements.txt` applied

The setup script (`scripts/setup.py`) verifies all of these and fails loudly if any are missing.

## Setup (once per machine)

1. Clone Kaizen locally (sibling to atelier, memex, agora):
   ```
   git clone https://github.com/nitekeeper/kaizen.git
   ```

2. Install Python dependencies:
   ```
   pip install -r requirements.txt
   ```

3. Run the setup script from Kaizen's root:
   ```
   PYTHONPATH=. python3 scripts/setup.py
   ```

   This verifies external dependencies and applies the schema migration to `.ai/memex.db`.

4. Register Kaizen as a local plugin in Claude Code (manual step — Kaizen is not distributed via Agora).

## DB and storage

| Path | Purpose | Tracked? |
|---|---|---|
| `~/.memex/` | Memex Brain — abandonment reports, cycle minutes, cross-repo learnings (managed by `memex:run`) | No (lives outside repo) |
| `.ai/memex.db` | Kaizen's project/run/cycle/abandonment state | No (gitignored, rebuilt via migrations) |
| `experiment/<owner>-<repo>/` | Ephemeral clone of the current target repo | No (gitignored, deleted after PR opens) |

Kaizen never writes to the target repo's working tree outside the experiment clone. The user's local copy of the target repo is never touched.

## Slash command

```
kaizen:improve <git-url> [--cycles N] [--subject "<area to improve>"]
```

This is the only user-invocable command. All other operations live in `internal/<name>/SKILL.md` and are reachable by agents via the Read tool.

## Working rules

1. **User-initiated only.** No agent may invoke `kaizen:improve` from another skill or script. Kaizen is run by humans.
2. **Bundled PR per run.** All cycles in one `kaizen:improve` invocation produce a single PR — successful cycles as commits, abandoned cycles as report references in the PR body.
3. **Skip-and-continue on abandonment.** A cycle that cannot reach completion (no consensus, all destructive rejected, tests unrecoverable, etc.) produces a formal report and the next cycle still runs.
4. **The clone is the work area.** All git operations happen in `experiment/<owner>-<repo>/`. The clone is destroyed after the PR opens, whether cycles succeeded or were all abandoned.
5. **Atelier infrastructure is reused, not duplicated.** Cycle agents invoke `/atelier:` slash commands; the 61-role roster is seeded into the clone's DB via `scripts/seed_roles.py` called as a subprocess. Kaizen does not vendor the agent profiles.
6. **Kaizen's own Memex stores cross-repo knowledge.** Abandonment reports and cycle minutes are captured to `.ai/wiki/` via `memex:run` (the Claude Code plugin). The user can query past runs via `memex:run ask`.

## Architecture pointers

- Design spec: `docs/design.md`
- Implementation plan: `docs/plan.md`
- Public slash command: `skills/improve/SKILL.md`
- Internal procedures: `internal/<name>/SKILL.md` (run, cycle, project, abandonment-report, etc.)
- Scripts: `scripts/*.py` — deterministic infrastructure (DB, git, clone, PR, detect)
- Migrations: `migrations/*.sql`
- Tests: `tests/test_*.py` — pytest

## Distribution

**Personal use only.** Kaizen is not published to Agora or PyPI. Install by cloning the repo and registering it manually with Claude Code.
