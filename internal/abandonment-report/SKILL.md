---
description: Use when a Kaizen cycle abandons — writes the formal markdown report, captures it to Kaizen's own memex, and records the abandonments row keyed to the cycle.
---

# internal/abandonment-report

When a cycle cannot complete, it produces a structured outcome with a reason code. This skill turns that outcome into a permanent record: a markdown report following design §4.5, captured to Kaizen's own wiki, with a matching row in the `abandonments` table.

Backed entirely by `scripts/abandonment.py` (which already exposes `format_report`, `capture_to_memex`, `record_abandonment`, and the end-to-end `process_abandonment` helper).

## Inputs

- `project` (dict) — the project row; needs `name` and `git_url`.
- `run_id` (int)
- `cycle_id` (int) — the id of the already-inserted `cycles` row (status `'abandoned'`). The orchestrator inserts that row before calling this skill.
- `cycle_n` (int)
- `subject` (str | None)
- `participants` (list[str]) — agent names.
- `phase_reached` (str) — one of `agenda`, `meeting`, `implementation`, `test`, `push`.
- `reason` (str) — one of `no_consensus`, `destructive_rejected`, `tests_unrecoverable`, `push_failed`, `other`.
- `detail` (str) — free-text. What was attempted, what blocked it, what the next session should reconsider.
- `artifacts` (list[str]) — slugs or paths of partial proposals, test logs, etc.

## Output

The inserted `abandonments` row dict (includes the assigned `id` and the `report_memex_slug`).

## Procedure

### Single-call path (recommended)

`scripts/abandonment.process_abandonment` does the whole sequence — format the markdown, attempt the memex capture (best-effort; warns and continues if `memex` is unavailable), and insert the abandonments row.

```
python -c "
import json
from scripts.abandonment import process_abandonment
project = json.loads('''<project JSON>''')
row = process_abandonment(
    db_path='.ai/memex.db',
    project=project,
    run_id=<run_id>,
    cycle_id=<cycle_id>,
    cycle_n=<cycle_n>,
    subject=<subject or None>,
    participants=<list of agent names>,
    phase_reached='<phase>',
    reason='<reason>',
    detail='''<detail>''',
    artifacts=<list of slugs/paths>,
)
print(json.dumps(row, default=str))
"
```

Parse the JSON on stdout and return as a dict.

### Step-by-step path (when the agent needs to inspect the markdown before capture)

If you need to review the rendered report before it goes to memex:

```
python -c "
from scripts.abandonment import format_report
md = format_report(
    project_name='<name>',
    git_url='<url>',
    run_id=<run_id>,
    cycle_n=<cycle_n>,
    subject=<subject or None>,
    participants=<list>,
    phase_reached='<phase>',
    reason='<reason>',
    detail='''<detail>''',
    artifacts=<list>,
)
print(md)
"
```

Inspect the output. If satisfied, capture it:

```
python -c "
from scripts.abandonment import capture_to_memex
slug = capture_to_memex('kaizen:abandonment:<run_id>-cycle-<cycle_n>', open('<tmp file with markdown>').read())
print(slug)
"
```

Then record the row:

```
python -c "
import json
from scripts.abandonment import record_abandonment
row = record_abandonment(
    db_path='.ai/memex.db',
    cycle_id=<cycle_id>,
    phase_reached='<phase>',
    reason='<reason>',
    detail='''<detail>''',
    report_memex_slug='<slug>',
)
print(json.dumps(row, default=str))
"
```

In practice the `process_abandonment` single-call path is what `internal/run/SKILL.md` invokes; the step-by-step is documented for debugging.

## Report format (reference)

`format_report` emits the canonical design §4.5 shape:

```markdown
---
id: kaizen:abandonment:<run_id>-cycle-<n>
title: Cycle <n> abandoned — <reason>
type: abandonment-report
project: <project name>
status: draft
---

Cycle: <n>
Date: YYYY-MM-DD HH:MM UTC
Subject: <subject or "PM-directed">
Participants: <comma-joined names or "(none recorded)">
Phase reached: <phase>
Reason for abandonment: <reason>
Detail: <free-text>
Artifacts: <comma-joined slugs or "(none)">

Repo: <git_url>
Run id: <run_id>
```

Do not edit this format ad hoc — it is the contract `format_report` enforces and `pr.py` expects when listing abandonment reports in the PR body.

## Hard rules

- **Slug format is fixed:** `kaizen:abandonment:<run_id>-cycle-<n>`. Anything else breaks cross-referencing in PR bodies and `memex ask` queries.
- **`memex capture` is best-effort.** If `memex` is not on PATH or the subprocess fails, the abandonment is still recorded with the slug stored — the report can be re-ingested by the user later. Never let a missing memex CLI block abandonment recording.
- **`phase_reached` and `reason` come from the cycle's structured outcome.** Do not invent values. If the cycle did not provide them, use `phase_reached="unknown"` and `reason="other"`.
- **The `cycles` row must already exist** when this skill is invoked. The orchestrator inserts it (with `status='abandoned'`) before calling this skill — `abandonments.cycle_id` references that row.
