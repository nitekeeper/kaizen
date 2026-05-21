---
description: Use when an agent needs to prepare or tear down a Kaizen experiment clone. Setup clones the target into `<kaizen-root>/experiment/<owner>-<repo>/` and seeds Atelier's schema + roles; teardown removes the directory.
---

# internal/clone-target

Wraps `scripts/clone.py` (clone + cleanup) and `scripts/seed_atelier_in_clone.py` (atelier schema + roles + wiki dir). The experiment clone is where every cycle's work happens; it is never the user's local copy of the target repo.

## Operations

- `setup <git-url>` — clone the target, configure git identity, seed atelier into the clone, ensure `.ai/wiki/` exists. Returns the clone directory path.
- `teardown <clone-dir>` — delete the clone directory. Safe on non-existent paths.

## Procedure

### setup

1. Parse owner and repo from the URL:

   ```
   python3 -c "
   from scripts.run import parse_owner_repo
   owner, repo = parse_owner_repo('<git-url>')
   print(owner, repo)
   "
   ```

   Both `https://github.com/owner/repo(.git)` and `git@github.com:owner/repo(.git)` forms are accepted.

2. Compute the clone directory: `<kaizen-root>/experiment/<owner>-<repo>/`. The kaizen root is the directory containing `scripts/` — the same `Path(__file__).resolve().parent.parent` that `scripts.run.kaizen_root()` returns.

3. Clone the repo:

   ```
   python3 scripts/clone.py clone <git-url> <branch> <clone-dir>
   ```

   - This invokes `git clone -b <branch> <git-url> <clone-dir>` then sets `user.email=kaizen@kaizen.local` / `user.name=Kaizen` in the clone.
   - `<branch>` is the target repo's base branch (e.g. `main`, `master`, `develop`, `trunk`) and is **required** — `clone_repo(remote_url, dest, branch)` has no default. The runtime path (`scripts.run.orchestrate_run`) reads it from the project record's `base_branch` field.
   - **Registration limitation**: `scripts/project.py:_register_cli` currently hardcodes `"main"` when cloning + creating the project row. Until task 19 (L2) lands a git-detected default branch, repos whose default is not `main` cannot be onboarded end-to-end via the registration flow — the operator must hand-edit the `projects.base_branch` column after registration before any run.
   - On failure (network, auth, bad URL): the subprocess raises `subprocess.CalledProcessError`. Surface the git error to the user and signal abort to the caller — do not proceed with a half-cloned directory. The caller (`internal/run/SKILL.md`) must not create a run row when setup fails.

4. Seed atelier into the clone:

   ```
   python3 scripts/seed_atelier_in_clone.py <clone-dir>
   ```

   This invokes atelier's `migrate.py` and `seed_roles.py` against `<clone>/.ai/memex.db`, then ensures `<clone>/.ai/wiki/` exists. Requires atelier to be on disk (verified by `scripts/setup.py`).

   On failure: surface the error. The clone exists at this point; the caller may either teardown immediately or preserve for inspection.

5. Return `clone_dir` as a `Path` object.

### teardown

1. Invoke cleanup:

   ```
   python3 scripts/clone.py cleanup <clone-dir>
   ```

   This calls `scripts.platform_utils.safe_rmtree`, which handles read-only files (common in `.git/objects/` on Windows). Safe to call when the directory does not exist.

2. Return None.

## When to call teardown

`internal/run/SKILL.md` calls `teardown` exactly once, **after** the PR opens (or after finalize-with-status='failed' if the user explicitly declined to preserve). The clone is preserved when:

- The push step fails — user may need to recover manually.
- The user has explicitly asked to inspect the clone (override).

In every other case, teardown runs at the end of the run.

## Hard rules

- **Always clone fresh.** Do not reuse an existing `experiment/<owner>-<repo>/` directory. If one exists from a prior aborted run, `safe_rmtree` it before re-cloning, or pick a different directory.
- **Never touch the user's local copy of the target repo.** The experiment clone is the only work area; the user's other checkouts must remain untouched.
- **Never push from the user's local copy.** Kaizen pushes from the experiment clone only.
- **Atelier seeding is mandatory.** Cycle agents depend on the 61-role roster being queryable from `<clone>/.ai/memex.db`. A clone without seeding will fail Phase 1 (participant resolution).
- **The clone is ephemeral by design.** Do not write cross-cycle state into it; persist state in the kaizen DB (`<kaizen-root>/.ai/memex.db`) instead.
