# Changelog

All notable changes to Kaizen are recorded here.

## [Unreleased]

### Added
- Design spec (`docs/design.md`) — locked architecture for multi-cycle improvement runs against any git repo via bundled GitHub PRs.
- Implementation plan (`docs/plan.md`) — 11-wave build order with parallel-eligible pairs marked.
- Plugin scaffolding: `CLAUDE.md`, `requirements.txt`, `.claude-plugin/plugin.json`, `.gitignore` (with editor + OS metadata patterns).

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
