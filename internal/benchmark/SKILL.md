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

## Cycle-3 extras

These are **additive** conveniences layered on the Cycle-1/2 engine; the before/after
procedure above is unchanged.

### Auto-generated scenarios (heuristic-first, no LLM required)

Instead of hand-writing `benchmark/scenarios/<name>.json`, kaizen can synthesize a
representative workload from the target's own docs:

```python
from scripts.tokenmeter_scenario import auto_generate_scenario

scenario = auto_generate_scenario("skills/improve")          # source="auto", NO LLM
# optional richer prompt via ONE injectable claude call (falls back to heuristic):
scenario = auto_generate_scenario("skills/improve", runner=real_claude_runner)
```

The default path is pure stdlib: it mines the target `SKILL.md`'s YAML `description`
plus its documented `## Usage` / `## Example` invocations into a deterministic,
mid-difficulty prompt (a **differentiation filter** keeps it neither trivial nor
impossible). The same skill dir always yields the same `prompt` + `scenario_hash`, so an
auto baseline is as comparable as a user one. Passing a `runner` (the injectable
headless-`claude` shape from `scripts/tokenmeter_run.py`) synthesizes a richer prompt via
one call; **any failure silently falls back to the heuristic**, and that synthesis call's
cost is excluded from the target measurement (the benchmark scopes by the target run's own
`session_id`). Regardless of source (`user` or `auto`), both feed the **same** measured
"set of interests" — auto-gen only changes how the prompt is produced, never what is
measured.

### Daily rollup CLI (`daily`) — the atelier feature-2 feed

```
PYTHONPATH=. python3 scripts/tokenmeter.py daily [--config-dir DIR] [--since YYYY-MM-DD]
```

Walks the transcript root (`--config-dir`, else `$CLAUDE_CONFIG_DIR` / `~/.claude`) and
emits the tokscale-compatible `to_daily_rollup`: one entry per **LOCAL-tz day × model**
with the four token categories kept split (`input_tokens` / `output_tokens` /
`cache_creation_input_tokens` / `cache_read_input_tokens`). `--since` keeps only days on
or after the given date (`unknown`-day buckets are retained — they can't be proven to
predate the cutoff). JSON to stdout; the walk is **read-only**.

### OckScore — OPTIONAL calibrated composite

`scripts/tokenmeter_schema.ockscore(outcome_score, total_tokens, *, lam=0.1, C=1e6)` =
`outcome_score − λ·ln(T/C)`. It augments — **never replaces** — the raw cost/token
figures, and surfaces as a clearly-labelled optional derived row
(`ockscore_optional_composite`) **only** when an `outcome_score` is present. `C` is
**recalibrated for kaizen scale** (`1e6`, vs OckBench's ~`1e4`) so the log term is ~0 at a
typical run. `T` is the **per-run-mean total** — the sum of the four per-run-mean category
figures (equivalently, the gross all-category token count divided by `n_runs`) — so it is
consistent with the headline per-category rows and the single-run `C=1e6` anchor holds at
any `N` (it is **not** the gross sum across runs, which would drift by `ln(N)`). Monotone by
construction: more tokens at equal outcome → lower score; a better outcome at fewer tokens →
higher.

**Reaching it from the CLI.** The `benchmark` subcommand supplies the `outcome_score` so the
row appears on a real run (otherwise the composite is dead in prod — nothing fed it):

- `--outcome-score FLOAT` sets it explicitly (a `0..1` unit-of-work outcome), or
- when omitted, it is **derived** from the run's outcome anchors:
  `base = 1.0 if --tests-green else 0.0`, scaled by the cycle-success ratio
  `--cycles-succeeded / (--cycles-succeeded + --cycles-abandoned)` when cycle counts are
  present. With **no** outcome info at all (tests not green and no cycle counts) no score is
  derived and the OPTIONAL row stays absent.

So `tokenmeter benchmark <scenario> --tests-green --cycles-succeeded 3` (or
`--outcome-score 0.9`) emits the ockscore row, while a bare run with no outcome flags does
not.

### Subagent-aggregation open question — status

Design §4 flags an OPEN QUESTION: does the Seam-A `claude --output-format json` result
object aggregate **subagent (sidechain)** usage, or only the orchestrator session?
Resolution stance for this build:

- **Seam B is ALWAYS authoritative** — the transcript walk INCLUDES sidechain tokens and
  is the headline total + per-agent/per-phase breakdown. Seam A is the cost oracle
  (validation only), never the headline.
- `tests/test_seam_a_aggregation.py` proves the reconciliation is **correct under BOTH
  regimes** (Seam-A-aggregates vs orchestrator-only): when the oracle covers only the
  orchestrator share, the gap is attributed to the `subagent-boundary` discriminator, not
  to `pricing`.
- The **live reconciliation residual** (`cost_oracle.divergence_cause` on a real run) is
  the production discriminator. We deliberately do **NOT** run an expensive live two-arm
  probe to settle the question abstractly — the residual answers it for free on every run.

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
