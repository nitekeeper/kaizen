# Changelog

All notable changes to Kaizen are recorded here.

## [Unreleased]

### Added
- Design spec (`docs/design.md`) — locked architecture for multi-cycle improvement runs against any git repo via bundled GitHub PRs.
- Implementation plan (`docs/plan.md`) — 11-wave build order with parallel-eligible pairs marked.
- Plugin scaffolding: `CLAUDE.md`, `requirements.txt`, `.claude-plugin/plugin.json`, `.gitignore` (with editor + OS metadata patterns).

## v0.2.0 — 2026-06-26

### Added
- **Token-usage benchmark (`tokenmeter`)** — measure a target skill/plugin's token usage, before vs. after a Kaizen improvement, in a uniform, comparable format. New `scripts/tokenmeter_*` package + CLI (`scripts/tokenmeter.py`) with five subcommands:
  - **`static`** — deterministic context-footprint of a skill/plugin (SKILL.md + tool/MCP schemas); char/4 approximation with an optional exact `count_tokens` path.
  - **`dynamic`** — real four-category usage (input / output / cache-write / cache-read) harvested from Claude Code transcripts (Seam B, the authoritative total) with verified two-stage dedup and subagent (sidechain) inclusion; the `claude --output-format json` `total_cost_usd` (Seam A) is used only as an independent cost oracle.
  - **`benchmark`** — runs a scenario N times (mean ± CV), `session_id`-scoped so it captures the run plus its subagents; combines static + dynamic into one report.
  - **`report`** — before/after delta with a control-vector gate that refuses to compare across drifted conditions (model, scenario, cycles, transport).
  - **`daily`** — tokscale-compatible per-day usage rollup feed.
- TTL-aware cache-write pricing (5m vs. 1h), per-figure `source`/`mode` labels, and an optional calibrated OckScore composite. See `internal/benchmark/SKILL.md` for the before → improve → after → delta procedure.

## v0.1.1 — 2026-06-24

### Added
- Release automation: `release.yml` now notifies the Agora marketplace on publish — it fires a `repository_dispatch` so Agora auto-opens a version-bump PR for kaizen.

### Changed
- Docs: record that Kaizen is registered in and distributed via Agora.

## v0.1.0 — TBD

First end-to-end release. Targets:
- `kaizen:improve <git-url>` slash command works against any registered project
- One bundled GitHub PR per run, with skip-and-continue abandonment handling
- Plugin-owned Memex captures abandonment reports + cycle minutes
- Atelier's `internal/self-improve/` removed (superseded by Kaizen)
