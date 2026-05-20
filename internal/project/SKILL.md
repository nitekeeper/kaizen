---
description: Use when an agent needs to register, look up, list, or edit a Kaizen project record. A project row stores the auto-detected (and user-confirmed) config used when running improvement cycles against the target.
---

# internal/project

CRUD wrapper around `scripts/project.py`. Most operations are thin shells over the CLI — the substance is in the registration flow, which clones the target, auto-detects config, and walks the user through confirming or editing before saving.

All operations use the kaizen DB at the default path `.ai/memex.db` (passed implicitly by the CLI).

## Operations

- `get-by-url <git-url>` — look up an existing project row.
- `get-or-register <git-url>` — return the existing row if any; otherwise walk through registration.
- `list` — list every registered project.
- `edit <id> --field=value [--field=value ...]` — update one or more fields on a project row.

(There is also `delete <id>` in `scripts/project.py` if the user explicitly requests it; not exposed as a default operation since runs cascade-delete from a project.)

## Procedure

### get-by-url

```
python3 scripts/project.py get-by-url <git-url>
```

- Exit 0 with JSON on stdout: parse and return as a dict.
- Exit 1 with "Not found" on stderr: return None.

The returned dict has these keys (notable ones): `id`, `git_url`, `name`, `base_branch`, `test_command`, `read_paths` (list), `expert_roster` (list of role ids), `language`, `registered_at`, `last_run_at`, `notes`.

### get-or-register

1. Try `get-by-url <git-url>`. If a row exists, return it immediately.
2. If null, inform the user:

   > "Project `<git-url>` is not registered. Cloning the repository to auto-detect language, test command, read paths, and expert roster..."

3. Invoke the interactive register flow:

   ```
   python3 scripts/project.py register <git-url>
   ```

   This subprocess:
   - Clones the target into a tempdir.
   - Runs `scripts/detect_config.detect_all()` to infer language, test command, read paths, and a default expert roster.
   - Prints the detected config and asks the user one of `(y)es confirm`, `(e)dit`, or `(n)o abort`.
   - On `e`, prompts for each field with the detected value shown as a default; empty input keeps the default.
   - On `y`, writes the row. On `n`, aborts without saving.
   - For unknown languages, refuses to save until the user provides `test_command` and `read_paths`.

   This is fully interactive — let the subprocess inherit stdin/stdout. Do not capture; the user must see and respond to the prompts.

4. Exit code:
   - `0` — registered (or pre-existing). Re-fetch with `get-by-url <git-url>` and return the row.
   - `1` — user aborted or required fields missing. Surface the message and signal abort to the caller (`internal/run/SKILL.md`); the run must not proceed.
   - `2` — clone failed (network, bad URL, auth). Surface the git error and signal abort.

### list

```
python3 scripts/project.py list
```

Returns a JSON array on stdout. Render as a table: `id | name | language | git_url | last_run_at`.

### edit

```
python3 scripts/project.py edit <id> --field=value [--field=value ...]
```

Updatable fields: `git_url`, `name`, `base_branch`, `test_command`, `read_paths`, `expert_roster`, `language`, `last_run_at`, `notes`.

For list fields (`read_paths`, `expert_roster`), the value must be a JSON array, e.g.:

```
--read_paths='["scripts/*.py", "skills/*/SKILL.md"]'
--expert_roster='["agent-systems-architect-1", "backend-engineer-1"]'
```

Returns the updated row on stdout as JSON.

## Hard rules

- **Always go through `get-or-register` from the run orchestrator** — never assume a project exists or silently create a default config.
- **Never bypass the user-confirm step in registration.** Auto-detect is a starting point; the user decides what gets saved.
- **`read_paths` and `expert_roster` are JSON arrays.** When passing them via `edit`, they must parse as JSON.
- **Delete cascades** — removing a project drops all its runs, cycles, and abandonment records via the FK ON DELETE CASCADE. Confirm with the user before invoking delete.
