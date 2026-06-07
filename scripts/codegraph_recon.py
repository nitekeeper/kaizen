"""Pre-cycle code-graph recon: build + ingest a code-nav graph of the target clone.

Before a run's cycles, this module builds an AST-only code-navigation graph of
the TARGET clone via the external ``graphify`` CLI (``graphify update <clone>
--no-cluster`` — deterministic, no LLM, no API key), then ingests the produced
``graph.json`` into Memex's ``~/.memex/code_graph.db`` (memex v2.9.0's
``scripts/code_graph.py``, reached via a PYTHONPATH-bridge subprocess), keyed by
repo identity ``owner/repo``. Phase 2 recon agents then navigate the graph
(where-is / callers / dependencies / neighbors / module-map) instead of
grep + full-file reads.

The whole feature is BEST-EFFORT and ON-by-default. It silently auto-skips
(logging one stderr note, returning a ``{"status": "skipped", ...}`` dict, and
NEVER raising) whenever any of these hold:

  - ``KAIZEN_CODEGRAPH`` is disabled (``0`` / ``false`` / ``no`` / ``off``);
  - ``graphify`` is not on PATH;
  - memex >= 2.9.0 (with ``scripts/code_graph.py``) cannot be resolved;
  - the graphify build or the ingest bridge subprocess fails.

DELIBERATE never-raise divergence from the codebase convention
================================================================
Sibling infra scripts (e.g. ``scripts/seed_atelier_in_clone.py``) RAISE on a
missing dependency — Atelier is a HARD dependency, so a missing Atelier must
abort the run loudly. graphify + memex>=2.9.0 are explicitly NON-hard
dependencies (``scripts/setup.py`` does NOT verify them); this recon is pure
acceleration for Phase 2. Therefore :func:`build_and_ingest` (and the
``find_memex_root`` resolver it calls) MUST NEVER raise — they return a skip
dict so the run continues unimpeded. A future maintainer must NOT "fix" this to
raise-on-failure: that would turn an optional accelerator into a run-aborting
hard dependency. The raise-on-failure convention applies only to hard deps.

Resolves memex's location from ``~/.memex/config.json`` (``plugin_root``, the
canonical pointer) first, then the Agora plugin cache
(``~/.claude/plugins/cache/agora/memex/<version>/``); see :func:`find_memex_root`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ── Feature gate ───────────────────────────────────────────────────────────

_CODEGRAPH_ENV = "KAIZEN_CODEGRAPH"


def _codegraph_enabled() -> bool:
    """Return True iff the code-graph recon is ON.

    DEFAULT ON — opt-out. Unset / empty / anything not in the falsey set ⇒ ON;
    only an explicit ``"0"`` / ``"false"`` / ``"no"`` / ``"off"`` (any case,
    whitespace-trimmed) turns it OFF. Mirrors :func:`team_executor._caveman_enabled`
    parsing but with the default INVERTED (caveman is opt-in; codegraph is
    opt-out).
    """
    raw = os.environ.get(_CODEGRAPH_ENV)
    if raw is None:
        return True  # unset → default ON
    val = raw.strip().lower()
    if val == "":
        return True  # empty → default ON
    return val not in ("0", "false", "no", "off")


# ── Locate memex (NON-hard dep — never raises) ─────────────────────────────

_AGORA_MEMEX = Path.home() / ".claude" / "plugins" / "cache" / "agora" / "memex"
_MEMEX_CONFIG = Path.home() / ".memex" / "config.json"
_MEMEX_MARKER = "scripts/code_graph.py"
_MEMEX_MIN_VERSION = (2, 9, 0)
# X.Y.Z prefix (tolerates a trailing pre-release/build suffix on the dir name).
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def _parse_version(name: str) -> tuple[int, int, int] | None:
    """Parse an ``X.Y.Z`` prefix from a version-dir name; None if unparseable.

    Robust to non-semver names (returns None so the caller skips them) and to a
    trailing suffix (e.g. ``2.9.0-rc1`` → ``(2, 9, 0)``).
    """
    m = _VERSION_RE.match(name.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _has_marker(candidate: Path) -> bool:
    return (candidate / _MEMEX_MARKER).exists()


def find_memex_root() -> Path | None:
    """Resolve memex's root (>= 2.9.0, with ``scripts/code_graph.py``).

    Resolution order:
      1. ``~/.memex/config.json`` → ``plugin_root`` (the canonical pointer):
         used iff it exists, carries the ``scripts/code_graph.py`` marker, and
         its dir name parses to >= 2.9.0.
      2. Otherwise scan ``~/.claude/plugins/cache/agora/memex/`` for the
         HIGHEST valid version directory carrying the marker.

    Returns the resolved root, or ``None`` on ANY miss. NEVER raises — memex
    >= 2.9.0 is a NON-hard dependency (see module docstring); a miss means
    "skip the recon", not "abort the run".
    """
    try:
        # 1. Canonical pointer from ~/.memex/config.json.
        if _MEMEX_CONFIG.exists():
            try:
                cfg = json.loads(_MEMEX_CONFIG.read_text(encoding="utf-8"))
                pinned = cfg.get("plugin_root")
                if pinned:
                    cand = Path(pinned)
                    ver = _parse_version(cand.name)
                    if (
                        cand.is_dir()
                        and _has_marker(cand)
                        and ver is not None
                        and ver >= _MEMEX_MIN_VERSION
                    ):
                        return cand
            except (OSError, ValueError):
                # Malformed config / unreadable pointer — fall through to scan.
                pass

        # 2. Scan the Agora plugin cache for the highest valid version.
        if not _AGORA_MEMEX.is_dir():
            return None
        best: tuple[tuple[int, int, int], Path] | None = None
        for child in _AGORA_MEMEX.iterdir():
            if not child.is_dir():
                continue
            ver = _parse_version(child.name)
            if ver is None or ver < _MEMEX_MIN_VERSION:
                continue
            if not _has_marker(child):
                continue
            if best is None or ver > best[0]:
                best = (ver, child)
        return best[1] if best is not None else None
    except Exception:
        return None


def _memex_env(memex_root: Path) -> dict[str, str]:
    """Return a credential-free environment dict for memex bridge subprocesses.

    Forwards only PATH, HOME, and locale/temp-dir vars; sets PYTHONPATH to the
    memex root so the bridge can ``from scripts import code_graph``. Never
    forwards session tokens, API keys, or other ambient credentials — those
    have no business reaching subprocesses loaded from a plugin cache. Copy of
    :func:`seed_atelier_in_clone._atelier_env`'s shape.
    """
    env: dict[str, str] = {}
    for key in ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TEMP", "TMP"):
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    env["PYTHONPATH"] = str(memex_root)
    return env


# ── Build + ingest (best-effort entry — NEVER raises) ──────────────────────

_GRAPHIFY_TIMEOUT_S = 300
_BRIDGE_TIMEOUT_S = 120

# Bridge script that ingests a graphify graph.json into memex's code_graph.db.
# Run as `python3 -c <this> <repo> <graph_path> <built_at_commit>` with
# PYTHONPATH=<memex_root> so `from scripts import code_graph` resolves. Emits a
# single JSON line to stdout: {"nodes": N, "edges": M}.
_INGEST_BRIDGE = (
    "import json, sys\n"
    "from scripts import code_graph\n"
    "repo, graph_path = sys.argv[1], sys.argv[2]\n"
    "bac = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] else None\n"
    "summary = code_graph.ingest_graph(repo, graph_path, built_at_commit=bac)\n"
    "print(json.dumps(summary))\n"
)

# Bridge script for the read-only query helpers. Run as
# `python3 -c <this> <fn> <repo> <arg...>`; calls code_graph.<fn>(repo, *args)
# and prints the JSON result. The fn name is validated against an allowlist
# BEFORE the subprocess is built, so it is never attacker-controlled.
_QUERY_BRIDGE = (
    "import json, sys\n"
    "from scripts import code_graph\n"
    "fn = getattr(code_graph, sys.argv[1])\n"
    "repo = sys.argv[2]\n"
    "args = sys.argv[3:]\n"
    "result = fn(repo, *args)\n"
    "print(json.dumps(result))\n"
)


def build_and_ingest(
    clone_dir: Path,
    owner_repo: str,
    *,
    built_at_commit: str | None = None,
) -> dict:
    """Build a code-nav graph of ``clone_dir`` and ingest it into memex.

    BEST-EFFORT, NEVER RAISES (see module docstring's never-raise divergence).
    Returns a status dict — ``{"status": "ingested", "nodes", "edges", "repo"}``
    on success, ``{"status": "skipped", "reason": ...}`` on any skip/failure.

    Sequence:
      1. gate off (``KAIZEN_CODEGRAPH`` disabled) → skip;
      2. ``graphify`` not on PATH → skip;
      3. memex >= 2.9.0 unresolvable → skip;
      4. ``graphify update <clone_dir> --no-cluster`` (no shell, check=False,
         timeout); graphify writes ``<clone_dir>/graphify-out/graph.json`` by
         default. We read it, ingest, then ``rmtree(<clone_dir>/graphify-out)``
         so the artifact NEVER reaches the PR diff (keeps the clone clean);
      5. ingest ``graph.json`` via the memex bridge subprocess
         (``code_graph.ingest_graph``);
      6. return the ingested summary.

    Any unexpected exception is caught and returned as a skip dict + a stderr
    note — the run must continue regardless.
    """
    try:
        if not _codegraph_enabled():
            return {"status": "skipped", "reason": "KAIZEN_CODEGRAPH disabled"}

        if shutil.which("graphify") is None:
            return {"status": "skipped", "reason": "graphify not on PATH"}

        memex_root = find_memex_root()
        if memex_root is None:
            return {"status": "skipped", "reason": "memex>=2.9.0 not found"}

        clone_dir = Path(clone_dir)
        graphify_out = clone_dir / "graphify-out"
        graph_json = graphify_out / "graph.json"

        # graphify writes its artifact INTO the clone (graphify-out/). We run a
        # throwaway temp dir as cwd so any incidental cwd-relative output lands
        # outside the clone, but graphify still targets <clone_dir>/graphify-out.
        tmp_cwd = tempfile.mkdtemp(prefix="kaizen-codegraph-")
        try:
            proc = subprocess.run(
                ["graphify", "update", str(clone_dir), "--no-cluster"],
                cwd=tmp_cwd,
                env=_memex_env(memex_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=_GRAPHIFY_TIMEOUT_S,
            )
            if proc.returncode != 0:
                return {
                    "status": "skipped",
                    "reason": f"graphify update failed (exit {proc.returncode})",
                }
            if not graph_json.exists():
                return {
                    "status": "skipped",
                    "reason": "graphify produced no graph.json",
                }

            # Ingest via the PYTHONPATH-bridge subprocess. No shell=True; the
            # interpreter is sys.executable; the inline script is a fixed
            # constant; repo / path / commit ride as argv (data, never code).
            ingest = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    _INGEST_BRIDGE,
                    owner_repo,
                    str(graph_json),
                    built_at_commit or "",
                ],
                env=_memex_env(memex_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=_BRIDGE_TIMEOUT_S,
            )
        finally:
            # Keep the clone clean: the graphify artifact must NEVER reach the
            # PR diff. Also drop the throwaway cwd.
            shutil.rmtree(graphify_out, ignore_errors=True)
            shutil.rmtree(tmp_cwd, ignore_errors=True)

        if ingest.returncode != 0:
            return {
                "status": "skipped",
                "reason": f"code_graph.ingest_graph failed (exit {ingest.returncode})",
            }
        try:
            summary = json.loads(ingest.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            return {
                "status": "skipped",
                "reason": "ingest bridge produced no parseable JSON summary",
            }
        return {
            "status": "ingested",
            "nodes": summary.get("nodes"),
            "edges": summary.get("edges"),
            "repo": owner_repo,
        }
    except Exception as exc:
        print(f"[codegraph_recon] best-effort skip: {exc}", file=sys.stderr)
        return {"status": "skipped", "reason": str(exc)}


# ── Read-only query helpers (Phase 2 nav) ──────────────────────────────────


def _query(memex_root: Path, fn: str, owner_repo: str, *args: str) -> object:
    """Shell to memex's ``code_graph.<fn>(owner_repo, *args)`` via the bridge.

    Returns the parsed-JSON child result (locations / file:line rows — NEVER
    file bodies). Raises on bridge failure; the CLI wrapper :func:`main`
    converts a failure into a clean JSON error status for the caller.
    """
    proc = subprocess.run(
        [sys.executable, "-c", _QUERY_BRIDGE, fn, owner_repo, *[str(a) for a in args]],
        env=_memex_env(memex_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=_BRIDGE_TIMEOUT_S,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"code_graph.{fn} bridge failed (exit {proc.returncode}): {proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def where_is(owner_repo: str, name: str) -> object:
    """Locate symbol ``name`` in ``owner_repo``; rows of locations (file:line)."""
    root = find_memex_root()
    if root is None:
        raise RuntimeError("memex>=2.9.0 not found")
    return _query(root, "where_is", owner_repo, name)


def callers(owner_repo: str, node_id: str) -> object:
    """Return callers of ``node_id`` in ``owner_repo`` (locations, not bodies)."""
    root = find_memex_root()
    if root is None:
        raise RuntimeError("memex>=2.9.0 not found")
    return _query(root, "callers", owner_repo, node_id)


def dependencies(owner_repo: str, node_id: str) -> object:
    """Return dependencies of ``node_id`` in ``owner_repo`` (locations)."""
    root = find_memex_root()
    if root is None:
        raise RuntimeError("memex>=2.9.0 not found")
    return _query(root, "dependencies", owner_repo, node_id)


def neighbors(owner_repo: str, node_id: str) -> object:
    """Return graph neighbors of ``node_id`` in ``owner_repo`` (locations)."""
    root = find_memex_root()
    if root is None:
        raise RuntimeError("memex>=2.9.0 not found")
    return _query(root, "neighbors", owner_repo, node_id)


def module_map(owner_repo: str, source_file: str) -> object:
    """Return the symbol map for ``source_file`` in ``owner_repo`` (locations)."""
    root = find_memex_root()
    if root is None:
        raise RuntimeError("memex>=2.9.0 not found")
    return _query(root, "module_map", owner_repo, source_file)


# ── CLI ────────────────────────────────────────────────────────────────────


def _resolve_repo(git_url_or_owner_repo: str) -> str:
    """Normalise a git URL OR an explicit ``owner/repo`` into ``owner/repo``."""
    arg = git_url_or_owner_repo.strip()
    # Bare owner/repo (no scheme, no `@`, single slash) — accept verbatim.
    if "://" not in arg and "@" not in arg and arg.count("/") == 1:
        return arg
    from scripts.run import parse_owner_repo

    owner, repo = parse_owner_repo(arg)
    return f"{owner}/{repo}"


def main(argv: list[str]) -> int:
    """CLI entry. stdout = clean JSON ONLY (agents parse it); diagnostics → stderr.

    Subcommands:
      build <clone_dir> <git_url_or_owner/repo>
      where-is <repo> <name>
      callers <repo> <node_id>
      deps <repo> <node_id>
      neighbors <repo> <node_id>
      module-map <repo> <source_file>
    """
    parser = argparse.ArgumentParser(
        prog="codegraph_recon",
        description="Pre-cycle code-graph recon (build + query).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser("build", help="build + ingest the code-nav graph")
    p_build.add_argument("clone_dir")
    p_build.add_argument("repo", help="git URL or owner/repo")
    p_build.add_argument("--built-at-commit", default=None)

    for name in ("where-is", "callers", "deps", "neighbors", "module-map"):
        p = sub.add_parser(name)
        p.add_argument("repo")
        p.add_argument("target", help="symbol / node_id / source_file")

    args = parser.parse_args(argv)

    try:
        if args.cmd == "build":
            owner_repo = _resolve_repo(args.repo)
            status = build_and_ingest(
                Path(args.clone_dir),
                owner_repo,
                built_at_commit=args.built_at_commit,
            )
            print(json.dumps(status))
            return 0

        owner_repo = _resolve_repo(args.repo)
        fn = {
            "where-is": where_is,
            "callers": callers,
            "deps": dependencies,
            "neighbors": neighbors,
            "module-map": module_map,
        }[args.cmd]
        result = fn(owner_repo, args.target)
        print(json.dumps(result))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "error", "reason": str(exc)}))
        print(f"[codegraph_recon] {args.cmd} failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
