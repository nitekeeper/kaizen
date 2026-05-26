# Cycle wall-clock budget

Operational runbook for the per-cycle wall-clock deadline (`CYCLE_WALL_S`) enforced by `scripts/cc_tool_bridge.py`'s `QueueBridgeWrapper`, and the `KAIZEN_CYCLE_WALL_S` operator escape hatch.

## What it is

`CYCLE_WALL_S` is a single outer wall-clock budget that bounds worst-case bridge time for **one cycle** of a `/kaizen:improve` run, irrespective of how many `bridge_requests` rows that cycle issues. Without this bound, a cycle that issues N dispatches can in principle block `PER_CALL_TIMEOUT_S * N` before any single-call timeout fires (e.g. `50 × 600s ≈ 8h`). The wall converts that pathological case into a deterministic abandonment.

Tripped via `BridgeStallError("cycle wall-clock exceeded")`. A trip aborts the cycle with the abandonment-report path; the next cycle in the run still starts fresh.

| Property | Value |
|---|---|
| Default | `3600.0` seconds (1 hour) |
| Module | `scripts/cc_tool_bridge.py` (resolved at import time) |
| Class attribute | `QueueBridgeWrapper.CYCLE_WALL_S` |
| Env override | `KAIZEN_CYCLE_WALL_S` |
| Test override | mutate `wrapper.CYCLE_WALL_S` per-instance |
| Failure mode | `BridgeStallError("cycle wall-clock exceeded: CYCLE_WALL_S=<n>")` |

The budget is **per-cycle, not per-run**. A 3-cycle run gets `3 × CYCLE_WALL_S` of cumulative bridge time at worst; each cycle's clock resets on its first `_request()` call.

## History

Introduced as a MINOR architect finding in issue #42 / PR review round 1 — without an outer wall, the chained `PER_CALL_TIMEOUT_S` budgets dominated. 3600s was chosen as generous-but-bounded: enough for legitimate multi-wave cycles (Phase 4 dispatches several parallel waves) while still capping the worst case.

Run 33 (2026-05-26) exposed the budget as too tight for certain cycle shapes. Cycle 1 of the portability-bundle run cleared **0-BLOCKING reviewers** with green tests, but the 3600s wall expired before `commit_cycle` / `push_branch` could fire. The run was hand-finished as PR #56 (`+855 / -4302`, 25 files); see the `project-kaizen-run-33-portability-bundle` memory entry for the post-mortem.

The `KAIZEN_CYCLE_WALL_S` env override was added directly in response — an operator escape hatch so a maintainer who knows a run will be long (large refactor, many waves, slow reviewers) can lift the wall without patching code or shipping a new PR. The default stays at 3600s because that catches genuine stalls; the override exists for the legitimate-but-slow case.

## The `KAIZEN_CYCLE_WALL_S` env variable

```
KAIZEN_CYCLE_WALL_S=<seconds>
```

Read **once at module import time** via `scripts.cc_tool_bridge._resolve_cycle_wall_s()`. In-process env mutation after import does not take effect — to change the budget mid-Python-process, override `wrapper.CYCLE_WALL_S` per-instance (this is the pattern the test suite uses). For an orchestrator-driven run, set the env var in the shell that launches the `/kaizen:improve` invocation.

### Defensive parsing — the rules

A malformed env var **MUST NOT abort a cycle**. Parsing in `_resolve_cycle_wall_s` falls back to the 3600s default on any input it cannot use, and warns to stderr so the operator sees what was rejected:

| `KAIZEN_CYCLE_WALL_S` value | Result | stderr warning |
|---|---|---|
| unset | `3600.0` (default) | none |
| `""` (empty string) | `3600.0` (default) | none |
| `"garbage"` (non-numeric) | `3600.0` (default) | yes — `is not numeric; falling back to default 3600.0s.` |
| `"0"` | `3600.0` (default) | yes — `must be > 0; falling back to default 3600.0s.` |
| `"-10"` (negative) | `3600.0` (default) | yes — `must be > 0; falling back to default 3600.0s.` |
| `"7200"` | `7200.0` | none |
| `"0.5"` | `0.5` | none (legitimate for tests / synthetic stalls) |
| `"86400"` (24h) | `86400.0` | none |

There is **no upper clamp** — a positive value is trusted as-is. The operator who sets the override is presumed to know what they are doing; an unbounded value is the whole point of the escape hatch.

## Operator examples

**Lift the wall to 2 hours for a known-long run:**

```bash
KAIZEN_CYCLE_WALL_S=7200 /kaizen:improve https://github.com/nitekeeper/some-repo --cycles 2
```

**Lift to 4 hours for a heavyweight refactor that previously timed out:**

```bash
KAIZEN_CYCLE_WALL_S=14400 /kaizen:improve https://github.com/nitekeeper/atelier \
  --subject "phase-5 substrate redesign" --mode team
```

**Drop to 60s for a smoke / synthetic-stall test:**

```bash
KAIZEN_CYCLE_WALL_S=60 python3 -m pytest tests/test_cc_tool_bridge.py -k cycle_wall
```

**Combine with `--mode team` (the env var setup that mode requires anyway):**

```bash
export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1
export KAIZEN_CYCLE_WALL_S=7200
/kaizen:improve https://github.com/nitekeeper/kaizen --mode team --cycles 3
```

## When to lift it (and when not to)

**Lift it when:**

- A previous run on the same target+subject hit the wall *after* tests went green (the run 33 shape — green work, lost to wall-clock).
- The cycle is known to dispatch many parallel waves (Phase 4 hand-orch with > 4 implementers + > 4 reviewers per wave).
- The target repo has slow CI (e.g. integration tests > 5min), which the implementer must wait on inside the cycle.

**Do NOT lift it when:**

- A run is hitting the wall *before* tests are green — that is a real stall, not a budget problem. Investigate the dispatch loop (heartbeat? Phase 3 mesh deadlock?) instead of raising the ceiling.
- The cycle keeps abandoning at Phase 1 (audit). Audit work is bounded; if it is not completing within 3600s, the agent is wedged, not slow.

## Related

- `scripts/cc_tool_bridge.py` — `_resolve_cycle_wall_s`, `CYCLE_WALL_S`, `QueueBridgeWrapper`.
- `tests/test_cc_tool_bridge.py` — `KAIZEN_CYCLE_WALL_S` env-override coverage and the synthetic deadline-trip test.
- `skills/improve/SKILL.md` — `### Environment variables` table (operator-facing summary).
- `project-kaizen-run-33-portability-bundle` Memex entry — originating incident.
- Issue #42 — architect MINOR finding that introduced `CYCLE_WALL_S`.
