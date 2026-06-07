# Runbook — Pre-cycle code-graph recon

## Overview

Before a `kaizen:improve` run's cycles begin, Kaizen builds an AST-only
code-navigation graph of the **target clone** and ingests it into Memex's
`~/.memex/code_graph.db`, keyed by repo identity `owner/repo`. Phase 2 recon
agents then navigate the graph (where-is / callers / dependencies / neighbors /
module-map) instead of falling back to grep + full-file reads, which keeps the
orchestrator's context lean and Phase 2's recon faster and more precise.

The graph is built by the external `graphify` CLI (`graphify update <clone>
--no-cluster`) — deterministic, AST-only, **no LLM and no API key**. The graph
is ingested via a PYTHONPATH-bridge subprocess into Memex v2.9.0's
`scripts/code_graph.py`.

Implementation: `scripts/codegraph_recon.py`. Wired into
`scripts.run.orchestrate_run` (Step 3.5, between seed and branch) and surfaced to
agents via `internal/run/SKILL.md` (Step 3.5) and `internal/cycle/SKILL.md`
(Phase 2).

## On-by-default + auto-skip matrix

The feature is **ON by default** and **best-effort**: `build_and_ingest` NEVER
raises. It logs one stderr note and returns a `{"status": "skipped", "reason":
...}` dict (the run continues unimpeded) in every one of these cases:

| Condition | Result |
|---|---|
| `KAIZEN_CODEGRAPH` unset / empty / any non-falsey value | recon runs (ON) |
| `KAIZEN_CODEGRAPH` = `0` / `false` / `no` / `off` (any case) | skip |
| `graphify` not on PATH | skip |
| memex >= 2.9.0 (with `scripts/code_graph.py`) not resolvable | skip |
| `graphify update` exits non-zero, or produces no `graph.json` | skip |
| ingest bridge subprocess fails / emits no parseable JSON | skip |
| any unexpected exception | skip (reason = the exception text) |

On success the status is `{"status": "ingested", "nodes": N, "edges": M, "repo":
"owner/repo"}`. `orchestrate_run` threads the built-vs-skipped signal to Phase 2
via the `KAIZEN_CODEGRAPH_AVAILABLE` env var; the team-mode `phase_2_audit.md`
template only renders the query-CLI guidance block when the graph was ingested.

### Never-raise design (deliberate)

Sibling infra scripts (e.g. `scripts/seed_atelier_in_clone.py`) **raise** on a
missing dependency because Atelier is a *hard* dependency. `graphify` and
memex >= 2.9.0 are explicitly **non-hard** dependencies — `scripts/setup.py`
does NOT verify them. This recon is pure acceleration, so `build_and_ingest`
(and `find_memex_root`) must never raise. Do not "fix" this to raise-on-failure;
that would turn an optional accelerator into a run-aborting hard dependency.

## Kill switch

Set `KAIZEN_CODEGRAPH=0` (or `false` / `no` / `off`) to disable the recon
entirely. The run proceeds exactly as before; Phase 2 agents fall back to grep.

## Prerequisites (for the feature to actually run)

These are NON-hard dependencies — absence is a silent skip, not an error:

- **`graphify`** on PATH (the AST graph builder).
- **memex >= 2.9.0** installed via Agora, carrying `scripts/code_graph.py` with
  `ingest_graph` + the query functions. Resolved from `~/.memex/config.json`'s
  `plugin_root` pointer first, else the highest valid version directory under
  `~/.claude/plugins/cache/agora/memex/`.

The clone is kept clean: graphify writes `<clone>/graphify-out/graph.json`, which
is removed after ingest so it never reaches the PR diff.

## Agent query commands

Run from the kaizen root (all return JSON locations — file:line rows, never file
bodies):

```
python3 scripts/codegraph_recon.py where-is   <owner/repo> <symbol>
python3 scripts/codegraph_recon.py callers    <owner/repo> <node_id>
python3 scripts/codegraph_recon.py deps       <owner/repo> <node_id>
python3 scripts/codegraph_recon.py neighbors  <owner/repo> <node_id>
python3 scripts/codegraph_recon.py module-map <owner/repo> <source_file>
```

Build (normally done automatically by `orchestrate_run` / Step 3.5):

```
python3 scripts/codegraph_recon.py build <clone_dir> <git_url_or_owner/repo>
```

`build` accepts either a git URL (https or ssh) or an explicit `owner/repo`. All
subcommands print clean JSON to stdout (agents parse it); diagnostics go to
stderr.
