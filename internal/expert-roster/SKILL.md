---
description: Use when an agent needs to resolve a project's `expert_roster` (list of role ids) to actual agent profiles. Read-only; queries the clone's seeded atelier DB.
---

# internal/expert-roster

Resolves the project-configured `expert_roster` (a JSON array of role/agent ids like `["agent-systems-architect-1", "backend-engineer-1"]`) into structured participant entries with name, role, and profile text. Agents in Phase 2/3 synthesize their personas from the profile.

The lookup runs against the **clone's** seeded DB (`<clone>/.ai/memex.db`), not Kaizen's own DB — Kaizen does not vendor the agent profiles; they live in atelier's seed and are written into each clone by `internal/clone-target/SKILL.md`.

## Inputs

- `roster_ids` (list[str]) — the project's `expert_roster` value.
- `clone_dir` (Path) — the experiment clone, already seeded with atelier's schema and roles.

## Output

A list of dicts, one per resolved id:

```python
[
  {
    "agent_id": "agent-systems-architect-1",
    "agent_name": "Dr. Nadia Petrov",
    "role_name": "Agent Systems Architect",
    "profile": "<full profile text from the agents.profile column>",
  },
  ...
]
```

If an id does not resolve (typo, missing seed), include an entry with `agent_name=None` and `role_name=None` and note the gap to the caller — but do not raise. Phase 1 logs the mismatch in the minutes and proceeds without that participant.

## Procedure

1. Compute the DB path: `<clone_dir>/.ai/memex.db`.

2. For each `agent_id` in `roster_ids`, query the seeded atelier DB:

   ```
   python -c "
   import json, sqlite3
   conn = sqlite3.connect(r'<clone_dir>/.ai/memex.db')
   conn.row_factory = sqlite3.Row
   rows = []
   for aid in <list of agent_ids>:
       cur = conn.execute(
           'SELECT a.id AS agent_id, a.name AS agent_name, a.profile, '
           '       r.role_name '
           'FROM agents a JOIN roles r ON a.role_id = r.id '
           'WHERE a.id = ?',
           (aid,)
       )
       row = cur.fetchone()
       if row is None:
           rows.append({'agent_id': aid, 'agent_name': None,
                        'role_name': None, 'profile': None})
       else:
           rows.append(dict(row))
   conn.close()
   print(json.dumps(rows))
   "
   ```

   The column names match atelier's schema: `agents(id TEXT PRIMARY KEY, name TEXT, role_id TEXT, profile TEXT)` joined to `roles(id TEXT PRIMARY KEY, role_name TEXT, ...)`.

3. Return the parsed list.

## Standing 6 + language-specific picks

The `expert_roster` stored on the project is already the merged list: standing 6 + any language-specific specialists (`scripts/detect_config.default_expert_roster` does the merge at registration time). This skill does not re-merge or re-pick; it resolves whatever ids the project record carries. If the user wants to change the roster, they edit the project via `internal/project/SKILL.md` (`edit`).

## Persona synthesis

The `profile` column carries the canonical persona text the agent should role-play during Phase 2 and Phase 3. When an agent is dispatched as a participant, the orchestrator passes that profile into the agent's working context as part of the briefing. The skill itself does not modify or summarise the profile — it returns the raw text.

## Hard rules

- **Read-only.** This skill does not insert, update, or seed agents. Seeding is `internal/clone-target/SKILL.md`'s job, done once per clone.
- **Resolve against the clone's DB, not Kaizen's.** Kaizen's `.ai/memex.db` has only the 5 kaizen tables; agent rows live in the per-clone seeded atelier DB.
- **Unknown ids do not raise.** Return a placeholder entry with nulls; let Phase 1 surface the gap in the minutes. Failing hard here would block the run on a typo that is easy to fix between cycles.
- **Do not mutate the resolved list across cycles.** Each cycle resolves fresh — agents may be added or removed by editing the project between runs, but within a run the roster is whatever it was when Phase 1 started.
