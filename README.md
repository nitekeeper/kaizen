# Kaizen

Kaizen is a **personal-use** Claude Code plugin. It runs Atelier's multi-agent
improvement methodology against any git repository specified by URL:

> clone → N improvement cycles → one bundled GitHub PR

The target repo is cloned into an ephemeral work area, improved over N independent
cycles, and the results ship as a single PR. **Your local copy of the target repo
is never touched** — all git operations happen inside Kaizen's throwaway clone,
which is deleted after the PR opens.

Successful cycles land as commits; cycles that can't reach completion are abandoned
gracefully (with a formal report referenced in the PR body) and the next cycle still
runs.

## Slash command

There is one user-invocable command:

```
kaizen:improve <git-url> [--cycles N] [--subject "<area to improve>"]
```

- `<git-url>` — required. https or ssh form
  (e.g. `https://github.com/owner/repo.git` or `git@github.com:owner/repo.git`).
- `--cycles N` — number of independent improvement cycles to run. Default: `1`.
- `--subject "..."` — optional focus area. If omitted, the PM agent decides per cycle.

All other operations are internal procedures (`internal/<name>/SKILL.md`) reachable by
agents, not invoked directly.

## Hard dependencies

Kaizen refuses to run if any of these are missing:

- **`git`** on PATH
- **`gh` CLI** on PATH and authenticated (`gh auth status` exits 0)
- **Atelier** installed via Agora (`atelier:run` skill available) — provides the
  61-role roster and dev-arc skills used by the cycle agents
- **Memex** installed via Agora (`memex:run` skill available) — used to capture
  abandonment reports and cycle minutes
- **Python 3.11+** with `pip install -r requirements.txt` applied

`scripts/setup.py` verifies all of these and fails loudly if any are missing.

## Setup (once per machine)

1. Clone Kaizen locally (sibling to `atelier`, `memex`, `agora`):

   ```
   git clone https://github.com/nitekeeper/kaizen.git
   ```

2. Install Python dependencies:

   ```
   pip install -r requirements.txt
   ```

3. Run the setup script from Kaizen's root — this verifies the external
   dependencies and applies the schema migration to `.ai/memex.db`:

   ```
   PYTHONPATH=. python3 scripts/setup.py
   ```

4. Install Kaizen via Agora (`kaizen` is registered in the marketplace), or
   register it as a local plugin in Claude Code.

## Distribution

**Personal use.** Kaizen is registered in Agora (install via the marketplace) but is
not published to PyPI. It can also be installed by cloning the repo and registering it
manually with Claude Code.

---

See [`CLAUDE.md`](CLAUDE.md) for the full operating charter, architecture pointers,
and working rules.
