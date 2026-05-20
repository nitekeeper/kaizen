# Changelog

All notable changes to Kaizen are recorded here.

## [Unreleased]

### Added
- Design spec (`docs/design.md`) — locked architecture for multi-cycle improvement runs against any git repo via bundled GitHub PRs.
- Implementation plan (`docs/plan.md`) — 11-wave build order with parallel-eligible pairs marked.
- Plugin scaffolding: `CLAUDE.md`, `requirements.txt`, `.claude-plugin/plugin.json`, `.gitignore` (with editor + OS metadata patterns).

## [0.1.0] — TBD

First end-to-end release. Targets:
- `kaizen:improve <git-url>` slash command works against any registered project
- One bundled GitHub PR per run, with skip-and-continue abandonment handling
- Plugin-owned Memex captures abandonment reports + cycle minutes
- Atelier's `internal/self-improve/` removed (superseded by Kaizen)
