---
description: Use when an agent acting as PM is facilitating the Phase 3 synthesis meeting of a Kaizen cycle. Structured debate of each agenda item with unanimous-or-DROPPED semantics; signals abandonment when every item drops.
---

# internal/synthesis-meeting

Phase 3 of a Kaizen cycle: the PM-facilitated synthesis meeting that turns parallel pre-analysis proposals into agreed Action Items. Adapts atelier's `internal/self-improve/SKILL.md` Phase 3 with explicit abandonment signalling.

## Inputs

- `agenda_items` (list[str]) — the numbered agenda from `internal/pm-agenda/SKILL.md`.
- `participants` (list[dict]) — resolved roster: `{"agent_name", "role_name", "profile", ...}` per entry.
- `proposals` (list[dict]) — Phase 2 output: per-agent proposals addressing one or more agenda items. Each entry at minimum: `{"agent_name", "agenda_item_n", "finding", "proposal", "risk", "anticipated_conflicts"}`.

## Output

A dict:

```python
{
  "outcome": "proceed" | "abandon",
  "minutes_markdown": "<the full Discussion + Decisions Log + Action Items sections>",
  "decisions": ["<decision 1>", "<decision 2>", ...],   # only when outcome=proceed
  "action_items": [
      {"description": "<what>", "files": ["<where>"], "assigned_to": "<agent_name>"},
      ...
  ],
  "dropped_items": [
      {"agenda_n": <n>, "reason": "<short summary of why no consensus>"},
      ...
  ],
}
```

When `outcome == "abandon"`, every agenda item was dropped — the caller (`internal/cycle/SKILL.md`) will return an abandonment outcome with `reason="no_consensus"`.

## Procedure

For each numbered agenda item, run a single debate round:

### Step A — Present proposals

List every proposal that addresses this agenda item:

> **Dr. [Name] ([Role]):** [Proposal summary, 1–3 sentences]

Include findings and risk classification when relevant. Do not paraphrase to the point of erasing disagreements.

### Step B — Solicit objections and support

Walk the participant list. Each agent gets one pass to:

- Support a proposal as-is.
- Raise an objection with reasoning (must be specific — "I disagree" without a why is not an objection).
- Propose a revision that addresses the objection.

### Step C — Revise toward unanimity

Iterate revisions until either:

- **Every present participant supports one revised version** — the item is **agreed**. Record the final wording and the assigned implementer(s).
- **An objection cannot be resolved within 2–3 revision rounds** — the item is **DROPPED**. Record the disagreement summary; no further attempts on this item this cycle.

Unanimous means every participant present. If a participant was dropped in Phase 2 (failed to respond), they do not block consensus — note their absence in the minutes.

### Step D — Record the outcome

For each agenda item, the minutes carry one of:

- `**Decision:** <agreed change> — *Unanimous*` plus an Action Item row.
- `**Decision:** DROPPED — <reason>` and no Action Item.

### After all items

Compose the minutes markdown in this format:

```markdown
## Discussion

### Agenda Item 1: [item text from agenda]
**Proposals:**
- Dr. [Name] ([Role]): [summary]
- ...

**Discussion:** [debate summary — what was contested, how revised]

**Decision:** [agreed text] — *Unanimous*
*or*
**Decision:** DROPPED — [why]

### Agenda Item 2: ...

## Decisions Log
1. [Decision text] — [file(s) affected]
2. ...

## Action Items
| # | Change | File(s) | Assigned to |
|---|---|---|---|
| 1 | [what] | [where] | [agent_name] |
| 2 | ... | ... | ... |
```

### Compute outcome

- If the Action Items table has at least one row: `outcome = "proceed"`.
- If every agenda item was DROPPED (Action Items table is empty): `outcome = "abandon"`. The minutes section is still complete and useful — it documents what was tried and why nothing landed. The caller captures it to memex via the abandonment report flow.

### Return

Return the output dict described under "Output" above. Include the decisions list (one entry per agreed item), the action items, and the dropped items.

## Hard rules

- **Unanimous or DROPPED.** No "majority rules," no "PM tiebreaker." If even one present participant objects to the final wording, the item drops.
- **Objections must have reasoning.** A participant who simply refuses to engage cannot block consensus; treat their non-response as absence and proceed without them (note in minutes).
- **Revisions, not new proposals.** Phase 3 refines what Phase 2 produced. If an agent wants to introduce a brand-new direction mid-meeting, that signals the agenda wasn't right — handle it as a single revision round; if it doesn't converge, drop and move on.
- **Even an all-DROPPED meeting produces complete minutes.** The minutes are the artifact the abandonment report cites — they must be readable on their own.
- **Every Action Item has exactly one assignee.** Multi-assignee items become two Action Items.
