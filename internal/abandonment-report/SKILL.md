---
description: Use when a Kaizen cycle abandons — renders the formal markdown report, writes it to .ai/wiki/<slug>.md, invokes memex:run capture, and records the abandonments row keyed to the cycle.
---

# internal/abandonment-report

When a cycle cannot complete, it produces a structured outcome with a reason code. This skill turns that outcome into a permanent record: a markdown report following design §4.5, written to `.ai/wiki/<slug>.md` and captured to Kaizen's own wiki via `memex:run capture`, with a matching row in the `abandonments` table.

Backed entirely by `scripts/abandonment.py` (which exposes `format_report`, `record_abandonment`, and the end-to-end `process_abandonment` helper — returning a `(row, markdown)` tuple).

## Inputs

- `project` (dict) — the project row; needs `name` and `git_url`.
- `run_id` (int)
- `cycle_id` (int) — the id of the already-inserted `cycles` row (status `'abandoned'`). The orchestrator inserts that row before calling this skill.
- `cycle_n` (int)
- `subject` (str | None)
- `participants` (list[str]) — agent names.
- `phase_reached` (str) — one of `agenda`, `meeting`, `implementation`, `test`, `review`, `push`. Use `review` for Phase 5b' fix-loop exhaustion (the independent-reviewer review-fix loop hit its max 5 iterations with unresolved findings); `push` is reserved for run-level push failures emitted by `internal/run/SKILL.md` Step 7.
- `reason` (str) — one of `no_consensus`, `destructive_rejected`, `tests_unrecoverable`, `review_unrecoverable`, `other`. Use `review_unrecoverable` when the Phase 5b' independent-reviewer fix loop exhausts its maximum 5 iterations with unresolved issues.
- `detail` (str) — free-text. What was attempted, what blocked it, what the next session should reconsider.
- `artifacts` (list[str]) — slugs or paths of partial proposals, test logs, etc.

## Output

A 2-tuple: the inserted `abandonments` row dict (includes the assigned `id` and the `report_memex_slug`), and the rendered markdown string. After calling this skill, the orchestrator writes the markdown to `.ai/wiki/<slug>.md` and invokes `memex:run capture` against that path.

## Procedure

### Single-call path (recommended)

`process_abandonment` renders the markdown and records the abandonments row; the caller is responsible for writing markdown to `.ai/wiki/<slug>.md` and invoking `memex:run capture`.

```
python3 -c "
import json
from scripts.abandonment import process_abandonment
project = json.loads('''<project JSON>''')
row, markdown = process_abandonment(
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

After the call, write the markdown and capture it:

```python
slug = row["report_memex_slug"]
from pathlib import Path
Path(".ai/wiki").mkdir(parents=True, exist_ok=True)
Path(f".ai/wiki/{slug}.md").write_text(markdown)
# Then invoke as a skill call:
# memex:run capture <slug> .ai/wiki/<slug>.md
```

### Step-by-step path (when the agent needs to inspect the markdown before capture)

If you need to review the rendered report before it goes to memex:

```
python3 -c "
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

Inspect the output. If satisfied, write the markdown to `.ai/wiki/<slug>.md`:

```python
slug = f"kaizen:abandonment:{run_id}-cycle-{cycle_n}"
from pathlib import Path
Path(".ai/wiki").mkdir(parents=True, exist_ok=True)
Path(f".ai/wiki/{slug}.md").write_text(md)
```

Then invoke `memex:run capture` as a skill call:

```
memex:run capture <slug> .ai/wiki/<slug>.md
```

Then record the row:

```
python3 -c "
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

### Review-loop structured fields (Phase 5b' only)

For `reason='review_unrecoverable'` abandonments produced by the Phase 5b' independent-reviewer fix loop, pass four additional keyword arguments to `process_abandonment` (or `record_abandonment` / `format_report`):

- `review_iteration_count` (int) — how many fix-loop iterations actually ran (max 5).
- `unresolved_findings` (list[dict]) — final unresolved issues, each `{reviewer, severity, finding, file_line}`. JSON-serialised in the DB; deserialised on read.
- `convergence_summary` (str) — one-paragraph explanation of why the fix loop couldn't converge.
- `reviewer_attribution` (dict) — `{finding_id: reviewer_role_id}` mapping linking each finding to the reviewer who raised it. JSON-serialised in the DB; deserialised on read.

All four default to `None` (omitted from the markdown report). Populate them ONLY for `review_unrecoverable`. For all other abandonment reasons, leave them unset — the rendered markdown will be identical to the legacy shape.

The authoritative contract is the `record_abandonment` / `process_abandonment` signatures in `scripts/abandonment.py`.

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
- **The caller's `memex:run capture` is best-effort.** If the wiki write or the skill invocation fails, the abandonment row is still recorded with the slug stored — the report can be re-ingested by the user later. Never let a missing memex plugin block abandonment recording.
- **`phase_reached` and `reason` come from the cycle's structured outcome.** Do not invent values. If the cycle did not provide them, the orchestrator (`scripts/run.py::orchestrate_run`) MUST raise `ValueError` *before* invoking this skill. Do not implement a fallback inside `process_abandonment` or `record_abandonment` — by the time control reaches the DB layer, the CHECK has already fired and the cycle's work is lost. The valid enums are mirrored in `scripts/abandonment.py::VALID_PHASES` and `VALID_REASONS` — keep them in sync with `migrations/004`.
- **The `cycles` row must already exist** when this skill is invoked. The orchestrator inserts it (with `status='abandoned'`) before calling this skill — `abandonments.cycle_id` references that row.
