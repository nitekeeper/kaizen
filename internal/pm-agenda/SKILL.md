---
description: Use when an agent acting as PM in a Kaizen cycle needs to produce an improvement agenda from the target repo. Reads the project's `read_paths`, summons participants from `expert_roster`, and produces a numbered agenda.
---

# internal/pm-agenda

Phase 1 of a Kaizen cycle: PM-led agenda setting. Adapts atelier's `internal/self-improve/SKILL.md` Phase 1 to read from per-project configuration (instead of hard-wiring to the atelier repo) and to use the kaizen-provided participant resolution.

## Inputs

- `clone_dir` (Path) — the experiment clone, on the run branch.
- `project` (dict) — the project row, including `name`, `git_url`, `read_paths`, `expert_roster`.
- `run_row` (dict) — the run row, including `subject` (which may be None).
- `cycle_n` (int) — 1-indexed cycle number within the run.

## Output

A markdown block of this shape:

```markdown
# Kaizen Cycle <n> Meeting — <project name>
**Date:** YYYY-MM-DD HH:MM UTC
**Facilitator:** Dr. Priya Nair (PM)
**Participants:**
| Agent | Role |
|---|---|
| Dr. [Name] | [Role] |
...

## PM Assessment *(only when subject is None)*
[Reasoning for chosen focus area — what the PM saw in the read_paths that prompted the chosen agenda]

## Agenda
1. [Improvement question — specific, answerable, scoped]
2. ...
```

Plus, in working context for the rest of the cycle: the list of resolved participants (each with `agent_id`, `agent_name`, `role_name`, `profile`).

If the PM produces zero agenda items (nothing in the read_paths warrants discussion this cycle), return an empty agenda — the caller (`internal/cycle/SKILL.md`) will abandon the cycle with `reason=other`.

## Procedure

### Step 1 — Record cycle start

Record the current UTC timestamp in working memory (the abandonment report and the minutes need it).

### Step 2 — Read the project config

The `project` dict carries:

- `read_paths` — JSON array of glob patterns relative to `clone_dir`, e.g. `["scripts/*.py", "skills/*/SKILL.md", "CLAUDE.md", "README.md"]`.
- `expert_roster` — JSON array of role ids (e.g., `["agent-systems-architect-1", "backend-engineer-1"]`).

### Step 3 — Read the target repo

From inside `clone_dir`, glob each pattern in `read_paths` and read every matching file. Use the Read tool against the absolute paths.

- If a pattern matches no files, note it in working memory (something is misconfigured) but do not abort. Continue with the files you did find.
- If the total content exceeds a sensible budget (say, 200kb), prioritize: `*.md` files first (skills, CLAUDE.md, README.md), then primary source, then tests.

### Step 4 — Resolve participants

Read `internal/expert-roster/SKILL.md` and follow its procedure with `project["expert_roster"]` and `clone_dir`. The result is a list of dicts: `{"agent_id", "agent_name", "role_name", "profile"}`. Hold this list — Phase 2 dispatches one agent per entry.

### Step 5 — Produce the agenda

Two branches:

**A. `run_row["subject"]` is provided.** Focus the agenda on the named subject. Produce 2–5 specific improvement questions framed against what you read in Step 3. Skip the "PM Assessment" section in the output (subject is the implicit focus statement).

**B. `run_row["subject"]` is None.** Audit the full set of read files and decide which area most needs improvement this cycle. Produce a short "PM Assessment" paragraph explaining the chosen focus and why (1–3 sentences). Then produce 2–5 specific improvement questions in that area.

Improvement questions should be:

- **Specific** — name files, sections, or behaviors.
- **Answerable** — the agents can take a position in Phase 2 and resolve it in Phase 3.
- **Scoped** — fits in one cycle. Don't include "rewrite the whole system from scratch" as one item.

### Step 6 — Render the agenda markdown

Use the template under "Output" above. Fill in:

- `<n>` — the cycle number.
- `<project name>` — `project["name"]`.
- Date as UTC `YYYY-MM-DD HH:MM UTC`.
- Participants table — one row per resolved participant (`agent_name` + `role_name`).
- PM Assessment paragraph (only branch B).
- Numbered agenda.

Return both the markdown block and the participant list.

## Hard rules

- **Read from `clone_dir`, not from any host repo.** The whole point of the clone is to isolate the work area; reading host files would couple kaizen runs to the developer's machine state.
- **Resolve participants from `project["expert_roster"]`, not from a hardcoded list.** Different projects need different rosters. The standing 6 are already merged into `expert_roster` by `scripts/detect_config.default_expert_roster` at registration time.
- **An empty agenda is a valid output** — it signals the caller to abandon. Do not fabricate filler agenda items just to keep the cycle alive.
- **The PM does not implement.** PM produces the agenda and facilitates the meeting (Phase 3). Implementation in Phase 4 is the assigned agents' job.
