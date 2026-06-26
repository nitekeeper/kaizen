---
description: Use when measuring a kaizen target skill/plugin's token cost before and after an improvement — write a scenario, capture a baseline, run the improvement, capture the post measurement, and render an attributable before/after delta with the control-vector gate.
---

# internal/benchmark

The token-usage benchmark's **before/after procedure**. It measures how much a kaizen
improvement changed the token cost of the **target** skill/plugin — once on the
unmodified target (baseline) and once on the improved target (post) — so the delta is
attributable to the edit and nothing else. It doubles as a regression gate: it proves
whether an edit made the target leaner or secretly more expensive (the "INERT levers" /
overthinking-tax failure mode kaizen has already lived).

Backed by the Cycle-1 measurement engine (`scripts/tokenmeter_*.py`) plus the Cycle-2
harness: `scripts/tokenmeter_scenario.py` (the fixed workload), `scripts/tokenmeter_run.py`
(the N-run dynamic runner), and the `benchmark` subcommand of `scripts/tokenmeter.py`.

## Two measurement modes (both folded into one report)

- **Static footprint** — deterministic, no run. Tokenizes what the target injects into
  context every time it is used (its `SKILL.md` description + body, the scripts/templates
  it references, plugin metadata). Clean signal, 1:1 with the edit, no variance.
- **Dynamic runtime** — actually runs the target on a fixed scenario `N` times (default
  `N=3`) and harvests the real four-category usage across the whole run (orchestrator +
  every sub-agent), reported as `{n, mean, cv}`. Agentic runs are non-deterministic, so
  the coefficient of variation (`cv`) is reported as a variable, never hidden.

`benchmark_target` (and the CLI `benchmark` subcommand) combine **both** into a single
`BenchmarkReport`: the static footprint becomes `overhead` rows, the N dynamic runs become
the four category rows plus per-phase and per-role rows. The four token categories are
NEVER summed into one total — `cache_read` dominates token COUNT while `output` dominates
COST, so a single "total" would be a lie.

## Procedure

### 1. Write a scenario

A scenario is the fixed, comparable workload: `{name, target, prompt}`. The `prompt` is a
representative, mid-difficulty invocation that exercises the target; `target` is the
skill/plugin directory measured for static footprint. Store it as a user-supplied JSON
file under `benchmark/scenarios/<name>.json` (see `benchmark/scenarios/improve.json` for a
real example targeting `skills/improve`). The runner computes a stable `scenario_hash` over
`prompt` + `target` (NOT the target's contents) — that hash is the comparability control.

### 2. Capture the baseline (BEFORE the improvement, unmodified target)

```
PYTHONPATH=. python3 scripts/tokenmeter.py benchmark benchmark/scenarios/improve.json \
  --model claude-opus-4-7 --transport host --effort high --n 3 \
  --target-commit "$(git rev-parse --short HEAD)" --out before.json
```

This replays the scenario prompt `N` times via headless `claude -p <prompt>
--output-format json`, harvests both seams, tags every record with its run / phase / cycle,
and writes the assembled static+dynamic report to `before.json`.

### 3. Run the improvement

Run the kaizen cycles (or the manual edit) that change the target. Hold the model, the
scenario, and the cycle count CONSTANT across before/after — only the target's contents
should change. The new version is recorded via `--target-commit`, which is NOT a control
(it is expected to differ).

### 4. Capture the post measurement (AFTER the improvement, improved target)

```
PYTHONPATH=. python3 scripts/tokenmeter.py benchmark benchmark/scenarios/improve.json \
  --model claude-opus-4-7 --transport host --effort high --n 3 \
  --target-commit "$(git rev-parse --short HEAD)" --out after.json
```

Same scenario, same model, same flags — so `before.json` and `after.json` share the same
`scenario_hash` / `model` / `effort` / `cycles` / `transport` / `rate_table_as_of` control
vector.

### 5. Render the attributable delta

```
PYTHONPATH=. python3 scripts/tokenmeter.py report before.json after.json --format md
```

The `report` subcommand pairs the rows and emits a `BEFORE | AFTER | Δ (abs + %)` view. Its
**control-vector gate** REFUSES the delta (raising `ControlDriftError`) if any control
drifted between the two reports — so a delta that renders is guaranteed to be attributable
to the improvement, not to a changed model / scenario / cycle count. A token win that
abandoned more cycles or shipped worse is not a win: read the delta alongside the report's
outcome footer (cycles succeeded/abandoned, PR opened, tests green).

## Outputs and downstream consumers

- **Canonical JSON** (`--format json`, the default) is the source of truth, with
  tokscale-compatible field names.
- **Markdown / CSV** (`--format md|csv`) is the human view rendered into the PR body.
- **Daily rollup** — `scripts/tokenmeter_render.to_daily_rollup` emits a per-day,
  per-model rollup (LOCAL-timezone `%Y-%m-%d`, four categories kept split) that feeds the
  future atelier daily token tracker.

Per kaizen's process-artifact policy these reports are gitignored; capture the canonical
JSON + per-call evidence to Memex (`memex:run capture`) and mirror the delta table into the
PR body. Query past benchmark runs via `memex:run ask`.

## Notes

- The headless `claude` subprocess is the ONLY external call and is INJECTABLE: tests pass
  a fake runner that returns canned result objects and writes canned transcripts, so the
  suite never spawns a real `claude`.
- A failed run (`is_error`, 0-byte, or `total_cost_usd == 0` with zero tokens) is treated
  as a FAILURE, never a $0 success — it surfaces in the run status rather than silently
  flattering the number.
- The Seam-A cost oracle (`total_cost_usd`) is reconciled against the Seam-B computed cost;
  a hard (>5%) divergence blocks the `validated` status and is attributed to either a
  sub-agent boundary or a pricing gap.
