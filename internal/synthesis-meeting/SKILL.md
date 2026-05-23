---
description: Use when an agent acting as PM is facilitating the Phase 3 synthesis meeting of a Kaizen cycle. Star → Mesh → Star agent-teams meeting that validates proposals, detects ripple effects, builds an Action Items DAG with waves, and signals abandonment when every item drops.
---

# internal/synthesis-meeting

Phase 3 of a Kaizen cycle: the PM-facilitated synthesis meeting that turns parallel pre-analysis proposals into a validated Action Items DAG with wave assignments. Replaces the old "PM reads proposals and writes synthesis alone" model with a **Star → Mesh → Star** agent-teams meeting pattern.

## Inputs

- `agenda_items` (list[str]) — the numbered agenda from `internal/pm-agenda/SKILL.md`.
- `participants` (list[dict]) — resolved roster: `{"agent_name", "role_name", "profile", ...}` per entry. **These are the same agents from Phase 2** — they carry through as teammates with full context of their own proposals and the proposals of every other agent.
- `proposals` (list[dict]) — Phase 2 output: per-agent proposals addressing one or more agenda items. Each entry: `{"agent_name", "agenda_item_n", "finding", "proposal", "risk", "anticipated_conflicts", "touches", "reads", "likely_depends_on"}`.

## Output

A dict:

```python
{
  "outcome": "proceed" | "abandon",
  "minutes_markdown": "<the full Discussion + Decisions Log + Action Items sections>",
  "decisions": ["<decision 1>", "<decision 2>", ...],   # only when outcome=proceed
  "action_items": [
      {
          "id": <int>,
          "description": "<what>",
          "touches": ["<file>", ...],
          "reads": ["<file>", ...],
          "owner": "<agent_name>",
          "depends_on": [<action_item_id>, ...],
          "wave": <int>,
      },
      ...
  ],
  "dropped_items": [
      {"agenda_n": <n>, "reason": "<short summary of why no consensus>"},
      ...
  ],
}
```

When `outcome == "abandon"`, every agenda item was dropped — the caller (`internal/cycle/SKILL.md`) will return an abandonment outcome with `reason="no_consensus"`.

## Meeting Responsibilities

The meeting has four responsibilities beyond reaching consensus:

1. **Validate proposals** — catch false positives. A proposal may be based on stale documentation rather than actual code; on a misread API signature; on an assumption another agent can immediately refute. Example: in run 6 a dev-qa hard-stop was triggered by a proposal grounded in outdated docs — a mesh exchange between the architect and the QA agent would have caught it before implementation. Agents must verify each other's factual premises, not just their recommendations.

2. **Detect ripple effects** — task A modifying a shared utility affects task B. The mesh phase surfaces these as explicit `depends_on` edges or as new Action Items when the effect is large enough.

3. **Build the DAG** — Action Items are grouped into waves by topological level of the dependency graph. Wave 1 has no predecessors; Wave 2 depends only on Wave 1; and so on. This DAG drives Phase 4 parallel implementation.

4. **Apply validation gates** before locking in (see below).

## Procedure

### Step 1 — Open (Star)

The lead (PM) `SendMessage`s each participant with:

- The full agenda (numbered list).
- Every agent's Phase 2 proposals in full — not just their own. Each agent sees the complete proposal set.
- The meeting goal: validate, debate, and converge to an agreed Action Items DAG.

Do not summarise or redact proposals. Participants need the unedited text to spot factual errors and conflicts.

### Step 2 — Debate (Mesh)

Participants `SendMessage` each other directly (not through the lead) to:

- **Validate factual premises** — challenge any proposal that appears to be based on stale docs, a wrong file reference, or a misread signature. Ask the proposer to cite the actual code line.
- **Surface false positives** — if a proposed problem does not exist in the actual code, flag it explicitly: "Proposal N claims X; I read the file and line Y shows Z — this looks like a false positive."
- **Detect ripple effects** — identify when two proposals touch the same file or when proposal A's output is required input for proposal B.
- **Debate conflicts** — when two proposals are mutually exclusive or in tension, work toward a reconciled version. Do not unilaterally defer; reach a shared position.

The lead observes via idle notifications and does not intervene unless the mesh stalls.

**Convergence bound:** The mesh phase ends when either:
- Every participant has sent at most **3 exchanges** since the Open, OR
- Every active participant has explicitly signalled **"I agree"** back to the lead.

Whichever occurs first. The lead may also call convergence early if the last round of exchanges produced no new objections or dependencies.

### Step 3 — Convergence

After the mesh ends, each participant sends the lead a short convergence summary:

- Which proposals they validate (no remaining objections).
- Which proposals they still object to — with a one-sentence reason.
- Any new ripple effects they discovered.
- Their updated `touches` / `reads` / `likely_depends_on` assessment for their own proposal.

### Step 4 — Close (Star)

The lead writes the consolidated meeting output:

#### 4a — Discussion section

For each agenda item, summarise:
- What was proposed.
- What was validated vs. challenged in the mesh.
- How conflicts were resolved (or why the item is being dropped).

#### 4b — Decisions Log

For each agreed item, one entry: `[Decision text] — [file(s) affected]`.

Items drop if even one present participant objects to the final wording after the mesh. Non-responsive participants (absent since Phase 2) do not block consensus — note their absence.

#### 4c — Action Items DAG

Assign each agreed decision an Action Item with these fields:

| # | Action | Touches | Reads | Owner | Depends on | Wave |
|---|---|---|---|---|---|---|

- **#** — sequential integer ID within this cycle.
- **Action** — concise description of the change.
- **Touches** — files this item modifies.
- **Reads** — files this item needs in a specific state (post-other-changes) before it can run.
- **Owner** — the agent who owns this item; carries forward to Phase 4 implementation (skin in the game — the agent who proposed and defended the item is the one implementing it).
- **Depends on** — predecessor Action Item IDs (empty if Wave 1).
- **Wave** — derived from topological level (Wave 1 = no predecessors, Wave 2 = depends only on Wave 1 items, etc.).

To compute waves: perform a topological sort on the dependency graph. Assign Wave 1 to all nodes with in-degree 0. Assign Wave N to all nodes whose longest dependency chain has N−1 steps.

### Step 5 — Validation Gates

Before the meeting locks in, check all four gates. If any gate fails, return to the mesh (Step 2) to resolve.

The 4 gates are mechanical — use the helper. Run it BEFORE posting the DAG to the shared task list:

```python
from scripts.dag import validate_dag, DAGValidationError
result = validate_dag(action_items, existing_files=frozenset(<files in clone>))
if not result.ok:
    # Surface every error in the meeting minutes Discussion section.
    # The PM negotiates with owners to fix; if no fix possible, the cycle
    # may abandon as no_consensus (per Phase 3's existing abandon path).
    for err in result.errors:
        post_to_discussion(f"DAG validation failed: {err}")
    if cycle_must_abandon:
        return abandon_outcome(...)
waves = result.waves  # use this for Phase 4 dispatch
```

The helper raises `ValueError` ONLY for malformed Action Item shapes (missing required keys) — those are agent bugs to fix in the source, not DAG-validation failures. The 4 validation gates produce `DAGValidationError` subclasses collected into `result.errors`.

The per-gate prose below is the contract the helper enforces:

1. **DAG is acyclic** — perform a topological sort; if a cycle is detected, identify the cycle members and ask the relevant owners to break it (typically by splitting one Action Item or reversing a `depends_on` edge).

2. **No file contention within a wave** — two Action Items in the same wave may not both appear in each other's `touches` for the same file. If contention is found, push the lower-priority item to the next wave by adding a `depends_on` edge.

3. **All `Reads` are satisfiable** — every file listed in a `reads` field must either exist in the current codebase OR be produced by an earlier wave's Action Item. If a `reads` entry is unsatisfiable, the owning agent must either remove the dependency or add a new Action Item to produce the file.

4. **No orphan dependencies** — every ID listed in a `depends_on` field must exist in the Action Items table. If an item depends on something nobody proposed, flag it and either add the missing item or remove the orphan edge.

### Step 6 — Compute Outcome and Return

**Compose the minutes markdown** in this format:

```markdown
## Discussion

### Agenda Item 1: [item text from agenda]
**Proposals:**
- Dr. [Name] ([Role]): [summary including touches/reads/likely_depends_on]
- ...

**Mesh summary:** [what was validated, what was challenged, how conflicts resolved]

**Decision:** [agreed text] — *Unanimous*
*or*
**Decision:** DROPPED — [why]

### Agenda Item 2: ...

## Decisions Log
1. [Decision text] — [file(s) affected]
2. ...

## Action Items
| # | Action | Touches | Reads | Owner | Depends on | Wave |
|---|---|---|---|---|---|---|
| 1 | [what] | [files] | [files] | [agent] | — | 1 |
| 2 | [what] | [files] | [files] | [agent] | 1 | 2 |
```

**Compute outcome:**

- If the Action Items table has at least one row: `outcome = "proceed"`.
- If every agenda item was DROPPED (Action Items table is empty): `outcome = "abandon"`. The minutes section is still complete and useful — it documents what was tried and why nothing landed.

Return the output dict described under "Output" above.

## Hard Rules

- **Unanimous or DROPPED.** No "majority rules," no "PM tiebreaker." If even one present participant objects to the final wording after the mesh, the item drops.
- **Objections must have reasoning.** A participant who simply refuses to engage cannot block consensus; treat their non-response as absence and proceed without them (note in minutes).
- **Validate facts, not just positions.** A proposal whose factual premise is proven wrong by actual code inspection is invalidated — not merely outvoted.
- **Ripple effects must surface during the mesh.** An agent who discovers a cross-task dependency mid-implementation cannot cite "I didn't know" — the mesh phase is exactly where this is supposed to surface.
- **Revisions, not new proposals.** Phase 3 refines what Phase 2 produced. A brand-new direction mid-meeting gets one revision round; if it doesn't converge, drop and move on.
- **All four validation gates must pass** before the meeting locks in. A gate failure returns the meeting to the mesh, not to Phase 2.
- **Every Action Item has exactly one Owner.** Multi-owner items become two Action Items.
- **Even an all-DROPPED meeting produces complete minutes.** The minutes are the artifact the abandonment report cites — they must be readable on their own.
- **Same agents from Phase 2 carry through.** Do not swap participants between Phase 2 and Phase 3. Continuity of context is the point — the agent who did the pre-analysis defends their proposal in the meeting.
