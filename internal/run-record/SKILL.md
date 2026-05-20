---
description: Use when an agent needs to create, update, finalize, or query a Kaizen run record. A run row tracks one multi-cycle invocation of `kaizen:improve`.
---

# internal/run-record

CRUD over the `runs` table in Kaizen's `.ai/memex.db`. Backed by `scripts/run.py`'s pure-Python functions (`create_run`, `finalize_run`, `get_run`, `list_runs`).

All operations use the kaizen DB at `.ai/memex.db`.

## Operations

- `create <project_id> <branch> <cycles_requested> [<subject>]` — insert a new `runs` row with `status='running'`.
- `update <run_id> [--branch=...]` — update mutable fields on an in-progress run (currently just `branch`, used when the orchestrator computes the branch after row creation; in practice we create with the final branch already known).
- `finalize <run_id> --cycles-succeeded=N --cycles-abandoned=M [--pr-url=...] [--status=complete|failed]` — write `ended_at`, cycle counts, optional PR URL, and final status.
- `get <run_id>` — fetch a run row.
- `list [--project-id=N]` — list runs, optionally filtered by project.

## Procedure

### create

```
python3 -c "
import json
from scripts.run import create_run
row = create_run(
    db_path='.ai/memex.db',
    project_id=<project_id>,
    branch='<branch>',
    cycles_requested=<n>,
    subject=<subject or None>,
)
print(json.dumps(row, default=str))
"
```

Parse the JSON on stdout and return as a dict. Notable fields: `id`, `project_id`, `branch`, `cycles_requested`, `started_at`, `status` (= `'running'`).

The orchestrator (`internal/run/SKILL.md`) records `run_id = row["id"]` for the rest of the run.

### update

Currently only `branch` is mutable mid-run. If needed:

```
python3 -c "
from scripts.db import get_connection
conn = get_connection('.ai/memex.db')
conn.execute('UPDATE runs SET branch = ? WHERE id = ?', ('<new_branch>', <run_id>))
conn.commit()
conn.close()
"
```

In the typical Wave 7 flow, the branch is computed before `create`, so `update` is rarely called. Documented here so the orchestrator has an out if needed.

### finalize

```
python3 -c "
import json
from scripts.run import finalize_run
row = finalize_run(
    db_path='.ai/memex.db',
    run_id=<run_id>,
    cycles_succeeded=<S>,
    cycles_abandoned=<A>,
    pr_url=<pr_url or None>,
    status='<complete or failed>',
)
print(json.dumps(row, default=str))
"
```

Sets `ended_at` to the current UTC ISO timestamp, writes the cycle counts, the PR URL (if known), and the final status. Returns the updated row.

### get

```
python3 -c "
import json
from scripts.run import get_run
row = get_run('.ai/memex.db', <run_id>)
print(json.dumps(row, default=str) if row else 'null')
"
```

Returns the row dict or `None`.

### list

```
python3 -c "
import json
from scripts.run import list_runs
print(json.dumps(list_runs('.ai/memex.db', <project_id or None>), default=str))
"
```

Returns a JSON array. Render as a table: `id | project_id | status | cycles_succeeded/cycles_abandoned/cycles_requested | branch | pr_url`.

## Hard rules

- **`status` is a CHECK-constrained column** — only `'running'`, `'complete'`, or `'failed'`. Any other value will fail the INSERT/UPDATE.
- **Always finalize.** Even when the run fails (push error, all cycles abandoned), call `finalize` with the appropriate status so `ended_at` is set and `cycles_succeeded`/`cycles_abandoned` reflect reality.
- **Do not insert cycle rows here.** The `cycles` and `abandonments` tables are written by `scripts/cycle.py` and `scripts/abandonment.py`; see `internal/cycle/SKILL.md` and `internal/abandonment-report/SKILL.md`.
- **Default DB path is `.ai/memex.db`** — relative to the kaizen repo root. The orchestrator is invoked from the kaizen root; do not pass a different path unless testing.
