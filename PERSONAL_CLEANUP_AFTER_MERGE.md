# Personal cleanup to apply after merge

This file documents the personal-side strikes that must happen in the SAME conversation
turn as this PR's merge, per the portability cleanup contract
(`docs/claude-operational-rules.md#F13`). Once executed, this file MUST be deleted in the
same cleanup commit so the repo stays a single source of truth.

Strikes are keyed by **rule name** (not line number) because line numbers drift between
draft, review, and merge. Each item cross-references the canonical rule ID in
`docs/claude-operational-rules.md`.

## How to use

1. Wait for this PR to merge.
2. In the same conversation turn as the merge notification, execute each section below.
3. Stage the strikes, verify with `git diff`, commit (and push to the personal config repo
   if applicable). Then delete this file from the kaizen repo with `git rm
   PERSONAL_CLEANUP_AFTER_MERGE.md` and commit.

## Section A — strikes in `~/.claude/CLAUDE.md`

- [ ] **Strike "Git Workflow" section** (personal CLAUDE.md). Migrated as `[P3]` in
  repo `## Claude operational rules` > `### Post-cycle`.
- [ ] **Strike "Model Usage" section** (personal CLAUDE.md). Migrated as the new
  `## Model recommendations` section in repo CLAUDE.md (per kaizen#54). The personal
  section can be removed entirely — kaizen's repo recommendation now carries the
  Opus-4.7 + high-effort posture for any kaizen-on-kaizen work, and other repos
  install their own model recommendations via their own CLAUDE.md.

(All other personal CLAUDE.md sections — Persona, Subagent Orchestration,
Context Management — STAY personal. Do not strike.)

## Section B — strikes in `~/.claude/projects/-home-nitekeeper-apps-kaizen/memory/`

Delete the following memory files (the rule they encode lives in repo
`## Claude operational rules` per the matrix in `docs/claude-operational-rules.md`):

- [ ] `feedback-kaizen-worker-checklist.md` (→ repo F1, `### Pre-flight`)
- [ ] `feedback-kaizen-cycle-verify.md` (→ repo F2, `### During cycle`)
- [ ] `feedback-kaizen-step6-must-fire.md` (→ repo F3, `### During cycle`)
- [ ] `feedback-kaizen-branch-name-source.md` (→ repo F4, `### During cycle`)
- [ ] `feedback-cc-team-mode-async-pattern.md` (→ repo F7, `### During cycle`)
- [ ] `feedback-cc-teamdelete-per-session.md` (→ repo F8, `### Post-cycle`)
- [ ] `feedback-atelier-orchestration-shortcuts.md` (→ repo F11, `### Target-repo work`)
- [ ] `feedback-delete-merged-branches.md` (→ repo F12, `### Post-cycle`)
- [ ] `feedback-portability-cleanup-contract.md` (→ repo F13, `### Post-cycle`)
- [ ] `feedback-reviewer-catches-implementer-misses.md` (→ folded into repo P2/F9,
  `### During-cycle` — review-fix loop bullet)

For MIXED memories — keep the file, but strike the kaizen-half paragraph(s) that
now live in repo:

- [ ] `feedback-personal-rules.md` — strike the "Subagent Orchestration" kaizen-shape
  subsection that moved to repo P2 (keep the general "always use subagents" preference,
  the persona pointer, and the Atelier-persona-lookup convention).
- [ ] `feedback-parallel-subagent-grouping.md` — strike the kaizen-Phase-4
  wave-dispatch paragraph that moved to repo F10 (keep the general hand-orchestration
  DAG heuristic applicable across all repos).

## Section C — strikes in `~/.claude/projects/-home-nitekeeper-apps-kaizen/memory/MEMORY.md`

For each deleted file in Section B (10 files), remove its index line from `MEMORY.md`.
Keep MIXED file entries (they still exist, just trimmed — update the description on
the index line if it no longer reflects what remains).

Index lines to remove:

- [ ] `[Kaizen worker pre-flight checklist]` → `feedback-kaizen-worker-checklist.md`
- [ ] `[Kaizen cycle implementer must mirror target-repo CI]` → `feedback-kaizen-cycle-verify.md`
- [ ] `[Kaizen Step 6 must fire between commit and push]` → `feedback-kaizen-step6-must-fire.md`
- [ ] `[Kaizen branch-name source-of-truth]` → `feedback-kaizen-branch-name-source.md`
- [ ] `[CC team-mode is async-only]` → `feedback-cc-team-mode-async-pattern.md`
- [ ] `[CC TeamDelete is per-session]` → `feedback-cc-teamdelete-per-session.md`
- [ ] `[Atelier orchestration shortcuts]` → `feedback-atelier-orchestration-shortcuts.md`
- [ ] `[Delete merged branches by default]` → `feedback-delete-merged-branches.md`
- [ ] `[Portability cleanup contract]` → `feedback-portability-cleanup-contract.md`
- [ ] `[Reviewer catches implementer misses]` → `feedback-reviewer-catches-implementer-misses.md`

## Section D — Notion mirror (if maintained)

- [ ] For each Section-B file you delete, mark the corresponding Notion Claude HQ →
  Decisions page as **"Superseded by kaizen/CLAUDE.md `## Claude operational rules`
  > `### <subsection>`"** with a link to the merged commit. Do not delete the Notion
  page (audit trail).
- [ ] For each MIXED file you trim, add a comment on the Decisions page noting which
  paragraph moved upstream and which remains personal-scope.

## Section E — final step

- [ ] `git rm PERSONAL_CLEANUP_AFTER_MERGE.md && git commit -m "chore: complete
  personal-rules cleanup after kaizen#NN merge"` in the kaizen repo. Open a small PR
  for that single deletion.

---

## NOT migrated — left intentionally in personal (do NOT strike these)

These items were audited and consciously kept personal. Per Phase 3 consensus, naming
them here prevents silent omissions from being misread as oversight.

- `feedback-kaizen-first-for-app-work` — maintainer invocation policy; migrating would
  coerce future kaizen installers to invoke kaizen for every app fix without consent.
- `feedback-mocks-must-match-reality` — general engineering principle, cross-domain.
- `feedback-github-bundled-close-syntax` — GitHub-tool knowledge, cross-repo.
- `feedback-github-close-keyword-scope` — GitHub-tool knowledge, cross-repo.
- `feedback-notion-claude-hq-auto-mirror` — leaks private Notion workspace IDs.
- `feedback-kaizen-base-branch` — lifted state, maintainer-arc-specific.
- `feedback-personal-rules` — the persona pointer line itself (distinct from the
  Subagent Orchestration subsection, which is trimmed per Section B above).
- All `project-*.md` files (ephemeral run ledgers + session-resume notes).
- `reference-notion-claude-hq.md` (exposes private workspace IDs).
- Personal `~/.claude/CLAUDE.md` sections: Persona, Subagent Orchestration,
  Context Management. (Model Usage is now migrated per Section A above.)
