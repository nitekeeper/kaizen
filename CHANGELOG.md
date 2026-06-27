# Changelog

All notable changes to Kaizen are recorded here.

## [Unreleased]

### Added
- Design spec (`docs/design.md`) — locked architecture for multi-cycle improvement runs against any git repo via bundled GitHub PRs.
- Implementation plan (`docs/plan.md`) — 11-wave build order with parallel-eligible pairs marked.
- Plugin scaffolding: `CLAUDE.md`, `requirements.txt`, `.claude-plugin/plugin.json`, `.gitignore` (with editor + OS metadata patterns).

## v0.2.1 — 2026-06-27

### Changed
- **Token-usage reduction across the cycle workflow** (kaizen-on-kaizen run 2, PR #14). Three behavior-preserving cuts, measured before/after with the deterministic footprint signal:
  - **Quiet the default Python test gate** — `detect_config.py` now emits `pytest -q --tb=short` instead of `-v`. A passing gate's captured output drops from ~115KB/~28.8k tokens to ~1.4KB/~354 tokens. Verdict stays returncode-based; `parse_pytest_pass_count` already handles the `-q` summary, and `--tb=short` preserves failure tracebacks.
  - **Cap green-gate retained output** — `ci_runner.run_ci_checks` now retains only the pytest summary line(s) on a PASS (new `_summarize_pass_output` + `_PYTEST_SUMMARY_RE`), while FAIL keeps full output verbatim for diagnosis. ~69KB → 81 B (−99.9%), independent of the target's `-v`/`-q` config.
  - **Remove the B1 terse/"caveman" per-spawn rule** — deleted `_TERSE_OUTPUT_RULE` + `_inject_terse_before_trailer` from `dispatch_templates.py` (and the now-dead anchor in `host_executor.py`), saving ~754 chars/~189 tokens per teammate spawn in prose/team mode. Mirrors atelier's removal of the same net-token-loss instruction; the F7 trailer renders byte-identically and the B2 `caveman_codec` digest path is unchanged.

  Combined, a green test gate drops from ~28.8k tokens to ~20 tokens regardless of the target repo's config. Net −135 LOC; full suite green.

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
