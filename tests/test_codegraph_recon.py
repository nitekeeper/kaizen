"""Tests for scripts/codegraph_recon.py — best-effort, never-raise code-graph recon."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from scripts import codegraph_recon

# ── Feature gate (env parse) ───────────────────────────────────────────────


class TestCodegraphEnabled:
    def test_unset_defaults_on(self, monkeypatch):
        monkeypatch.delenv(codegraph_recon._CODEGRAPH_ENV, raising=False)
        assert codegraph_recon._codegraph_enabled() is True

    def test_empty_defaults_on(self, monkeypatch):
        monkeypatch.setenv(codegraph_recon._CODEGRAPH_ENV, "")
        assert codegraph_recon._codegraph_enabled() is True

    @pytest.mark.parametrize("val", ["0", "false", "FALSE", "no", "No", "off", "OFF", " off "])
    def test_falsey_values_off(self, monkeypatch, val):
        monkeypatch.setenv(codegraph_recon._CODEGRAPH_ENV, val)
        assert codegraph_recon._codegraph_enabled() is False

    @pytest.mark.parametrize("val", ["1", "true", "yes", "on", "anything-else"])
    def test_other_values_on(self, monkeypatch, val):
        monkeypatch.setenv(codegraph_recon._CODEGRAPH_ENV, val)
        assert codegraph_recon._codegraph_enabled() is True


# ── Version parsing ────────────────────────────────────────────────────────


class TestParseVersion:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("2.9.0", (2, 9, 0)),
            ("2.10.1", (2, 10, 1)),
            ("2.9.0-rc1", (2, 9, 0)),
            ("3.0.0+build", (3, 0, 0)),
        ],
    )
    def test_parseable(self, name, expected):
        assert codegraph_recon._parse_version(name) == expected

    @pytest.mark.parametrize("name", ["latest", "main", "", "v2.9.0", "2.9"])
    def test_unparseable_returns_none(self, name):
        assert codegraph_recon._parse_version(name) is None


# ── find_memex_root — never raises, version-floor, config pointer ──────────


class TestFindMemexRoot:
    def test_no_config_no_cache_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(codegraph_recon, "_MEMEX_CONFIG", tmp_path / "nope.json")
        monkeypatch.setattr(codegraph_recon, "_AGORA_MEMEX", tmp_path / "nocache")
        assert codegraph_recon.find_memex_root() is None

    def test_config_pointer_used_when_valid(self, monkeypatch, tmp_path):
        root = tmp_path / "2.9.0"
        (root / "scripts").mkdir(parents=True)
        (root / "scripts" / "code_graph.py").write_text("# memex\n")
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"plugin_root": str(root)}))
        monkeypatch.setattr(codegraph_recon, "_MEMEX_CONFIG", cfg)
        assert codegraph_recon.find_memex_root() == root

    def test_config_pointer_rejected_below_floor(self, monkeypatch, tmp_path):
        root = tmp_path / "2.8.0"
        (root / "scripts").mkdir(parents=True)
        (root / "scripts" / "code_graph.py").write_text("# memex\n")
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"plugin_root": str(root)}))
        monkeypatch.setattr(codegraph_recon, "_MEMEX_CONFIG", cfg)
        # No cache fallback present → None.
        monkeypatch.setattr(codegraph_recon, "_AGORA_MEMEX", tmp_path / "nocache")
        assert codegraph_recon.find_memex_root() is None

    def test_config_pointer_missing_marker_falls_through(self, monkeypatch, tmp_path):
        root = tmp_path / "2.9.0"
        root.mkdir()  # no scripts/code_graph.py marker
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"plugin_root": str(root)}))
        monkeypatch.setattr(codegraph_recon, "_MEMEX_CONFIG", cfg)
        monkeypatch.setattr(codegraph_recon, "_AGORA_MEMEX", tmp_path / "nocache")
        assert codegraph_recon.find_memex_root() is None

    def test_cache_scan_picks_highest_valid_version(self, monkeypatch, tmp_path):
        cache = tmp_path / "cache"
        cache.mkdir()
        for v in ("2.8.0", "2.9.0", "2.10.0", "garbage"):
            d = cache / v
            (d / "scripts").mkdir(parents=True)
            (d / "scripts" / "code_graph.py").write_text("# memex\n")
        monkeypatch.setattr(codegraph_recon, "_MEMEX_CONFIG", tmp_path / "nope.json")
        monkeypatch.setattr(codegraph_recon, "_AGORA_MEMEX", cache)
        assert codegraph_recon.find_memex_root() == cache / "2.10.0"

    def test_malformed_config_does_not_raise(self, monkeypatch, tmp_path):
        cfg = tmp_path / "config.json"
        cfg.write_text("{not json")
        monkeypatch.setattr(codegraph_recon, "_MEMEX_CONFIG", cfg)
        monkeypatch.setattr(codegraph_recon, "_AGORA_MEMEX", tmp_path / "nocache")
        # Must not raise — falls through to (absent) cache scan → None.
        assert codegraph_recon.find_memex_root() is None


# ── _memex_env — credential-free ───────────────────────────────────────────


class TestMemexEnv:
    def test_forwards_only_safe_keys_and_sets_pythonpath(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("HOME", "/home/x")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
        monkeypatch.setenv("GH_TOKEN", "secret2")
        env = codegraph_recon._memex_env(tmp_path)
        assert env["PYTHONPATH"] == str(tmp_path)
        assert env.get("PATH") == "/usr/bin"
        assert "ANTHROPIC_API_KEY" not in env
        assert "GH_TOKEN" not in env


# ── build_and_ingest — best-effort skips, never raises ─────────────────────


class TestBuildAndIngestSkips:
    def test_skip_when_disabled(self, monkeypatch, tmp_path):
        monkeypatch.setenv(codegraph_recon._CODEGRAPH_ENV, "0")
        out = codegraph_recon.build_and_ingest(tmp_path, "o/r")
        assert out["status"] == "skipped"
        assert "KAIZEN_CODEGRAPH" in out["reason"]

    def test_skip_when_graphify_absent(self, monkeypatch, tmp_path):
        monkeypatch.delenv(codegraph_recon._CODEGRAPH_ENV, raising=False)
        monkeypatch.setattr(codegraph_recon.shutil, "which", lambda n: None)
        out = codegraph_recon.build_and_ingest(tmp_path, "o/r")
        assert out["status"] == "skipped"
        assert "graphify" in out["reason"]

    def test_skip_when_memex_unresolvable(self, monkeypatch, tmp_path):
        monkeypatch.delenv(codegraph_recon._CODEGRAPH_ENV, raising=False)
        monkeypatch.setattr(codegraph_recon.shutil, "which", lambda n: "/usr/bin/graphify")
        monkeypatch.setattr(codegraph_recon, "find_memex_root", lambda: None)
        out = codegraph_recon.build_and_ingest(tmp_path, "o/r")
        assert out["status"] == "skipped"
        assert "memex" in out["reason"]

    def test_never_raises_on_subprocess_returncode_1(self, monkeypatch, tmp_path):
        monkeypatch.delenv(codegraph_recon._CODEGRAPH_ENV, raising=False)
        monkeypatch.setattr(codegraph_recon.shutil, "which", lambda n: "/usr/bin/graphify")
        monkeypatch.setattr(codegraph_recon, "find_memex_root", lambda: tmp_path)

        def fake_run(*a, **k):
            return CompletedProcess(args=a, returncode=1, stdout="", stderr="boom")

        monkeypatch.setattr(codegraph_recon.subprocess, "run", fake_run)
        out = codegraph_recon.build_and_ingest(tmp_path, "o/r")
        assert out["status"] == "skipped"

    def test_never_raises_when_subprocess_raises(self, monkeypatch, tmp_path):
        monkeypatch.delenv(codegraph_recon._CODEGRAPH_ENV, raising=False)
        monkeypatch.setattr(codegraph_recon.shutil, "which", lambda n: "/usr/bin/graphify")
        monkeypatch.setattr(codegraph_recon, "find_memex_root", lambda: tmp_path)

        def boom(*a, **k):
            raise OSError("subprocess exploded")

        monkeypatch.setattr(codegraph_recon.subprocess, "run", boom)
        out = codegraph_recon.build_and_ingest(tmp_path, "o/r")
        assert out["status"] == "skipped"
        assert "subprocess exploded" in out["reason"]

    def test_skip_when_no_graph_json_produced(self, monkeypatch, tmp_path):
        monkeypatch.delenv(codegraph_recon._CODEGRAPH_ENV, raising=False)
        monkeypatch.setattr(codegraph_recon.shutil, "which", lambda n: "/usr/bin/graphify")
        monkeypatch.setattr(codegraph_recon, "find_memex_root", lambda: tmp_path)

        def fake_run(argv, **k):
            # graphify "succeeds" but writes no graph.json
            return CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(codegraph_recon.subprocess, "run", fake_run)
        out = codegraph_recon.build_and_ingest(tmp_path, "o/r")
        assert out["status"] == "skipped"
        assert "graph.json" in out["reason"]


class TestBuildAndIngestHappyPath:
    def test_ingested_with_parsed_counts(self, monkeypatch, tmp_path):
        monkeypatch.delenv(codegraph_recon._CODEGRAPH_ENV, raising=False)
        # Canary credentials: must NOT leak into the graphify subprocess env.
        monkeypatch.setenv("GH_TOKEN", "ghp_canary-token")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-canary-key")
        monkeypatch.setattr(codegraph_recon.shutil, "which", lambda n: "/usr/bin/graphify")
        memex_root = tmp_path / "memex"
        memex_root.mkdir()
        monkeypatch.setattr(codegraph_recon, "find_memex_root", lambda: memex_root)

        clone = tmp_path / "clone"
        clone.mkdir()
        graph_json = clone / "graphify-out" / "graph.json"

        calls: list[tuple[list, dict]] = []

        def fake_run(argv, **k):
            calls.append((argv, k))
            if argv[0] == "graphify":
                # Simulate graphify writing the artifact into the clone.
                graph_json.parent.mkdir(parents=True, exist_ok=True)
                graph_json.write_text("{}")
                return CompletedProcess(args=argv, returncode=0, stdout="", stderr="")
            # The ingest bridge.
            return CompletedProcess(
                args=argv,
                returncode=0,
                stdout='{"nodes":5,"edges":7,"repo":"o/r"}',
                stderr="",
            )

        monkeypatch.setattr(codegraph_recon.subprocess, "run", fake_run)
        out = codegraph_recon.build_and_ingest(clone, "o/r", built_at_commit="HEAD")
        assert out == {"status": "ingested", "nodes": 5, "edges": 7, "repo": "o/r"}

        graphify_call = next(c for c in calls if c[0][0] == "graphify")
        bridge_call = next(c for c in calls if c[0][0] == sys.executable)
        g_argv, g_kw = graphify_call
        b_argv, b_kw = bridge_call

        # The bridge argv uses sys.executable and the -c inline script, no shell.
        assert b_argv[1] == "-c"
        assert "o/r" in b_argv
        # REGRESSION (bridge cwd): the `-c` memex-import bridge MUST run with
        # cwd == memex_root, else sys.path[0] ("" == caller cwd) shadows it and
        # `from scripts import code_graph` imports the caller's scripts/ package
        # (kaizen has one). This was the production failure the mocked happy-path
        # missed. Both PYTHONPATH and cwd must point at memex.
        assert b_kw["cwd"] == str(memex_root)
        assert b_kw["env"]["PYTHONPATH"] == str(memex_root)
        # REGRESSION (graphify env + abs path): graphify is a standalone tool —
        # it must NOT be handed the scrubbed memex PYTHONPATH, and the target
        # path must be absolute (it runs in a different cwd).
        assert g_argv[2] == str(clone.resolve())
        assert g_kw["env"].get("PYTHONPATH") != str(memex_root)
        # SECURITY (graphify env allowlist): graphify processes an UNTRUSTED
        # clone — ambient credentials must not reach it. PATH must still be
        # forwarded verbatim (allowlisted, not over-scrubbed), and PYTHONPATH
        # must never be set at all (PR #102: forcing memex's PYTHONPATH broke
        # graphify).
        assert "GH_TOKEN" not in g_kw["env"], "GH_TOKEN leaked into graphify env"
        assert "ANTHROPIC_API_KEY" not in g_kw["env"], "API key leaked into graphify env"
        assert g_kw["env"]["PATH"] == os.environ["PATH"]
        assert "PYTHONPATH" not in g_kw["env"]
        # Clone kept clean: graphify-out removed after ingest.
        assert not (clone / "graphify-out").exists()

    def test_bridge_no_shell_true(self, monkeypatch, tmp_path):
        monkeypatch.delenv(codegraph_recon._CODEGRAPH_ENV, raising=False)
        monkeypatch.setattr(codegraph_recon.shutil, "which", lambda n: "/usr/bin/graphify")
        memex_root = tmp_path / "memex"
        memex_root.mkdir()
        monkeypatch.setattr(codegraph_recon, "find_memex_root", lambda: memex_root)
        clone = tmp_path / "clone"
        clone.mkdir()
        graph_json = clone / "graphify-out" / "graph.json"

        kwargs_seen: list[dict] = []

        def fake_run(argv, **k):
            kwargs_seen.append(k)
            if argv[0] == "graphify":
                graph_json.parent.mkdir(parents=True, exist_ok=True)
                graph_json.write_text("{}")
                return CompletedProcess(args=argv, returncode=0, stdout="", stderr="")
            return CompletedProcess(
                args=argv, returncode=0, stdout='{"nodes":1,"edges":1}', stderr=""
            )

        monkeypatch.setattr(codegraph_recon.subprocess, "run", fake_run)
        codegraph_recon.build_and_ingest(clone, "o/r")
        assert all("shell" not in k for k in kwargs_seen)

    def test_skip_when_ingest_bridge_returncode_nonzero(self, monkeypatch, tmp_path):
        """graphify succeeds + graph.json exists, but the ingest bridge returns
        rc!=0 -> skipped with the ingest-failed reason (never raises). Covers the
        ingest-rc branch distinctly from the graphify-rc branch."""
        monkeypatch.delenv(codegraph_recon._CODEGRAPH_ENV, raising=False)
        monkeypatch.setattr(codegraph_recon.shutil, "which", lambda n: "/usr/bin/graphify")
        memex_root = tmp_path / "memex"
        memex_root.mkdir()
        monkeypatch.setattr(codegraph_recon, "find_memex_root", lambda: memex_root)
        clone = tmp_path / "clone"
        clone.mkdir()
        graph_json = clone / "graphify-out" / "graph.json"

        def fake_run(argv, **k):
            if argv[0] == "graphify":
                graph_json.parent.mkdir(parents=True, exist_ok=True)
                graph_json.write_text("{}")
                return CompletedProcess(args=argv, returncode=0, stdout="", stderr="")
            # The ingest bridge fails.
            return CompletedProcess(args=argv, returncode=1, stdout="", stderr="boom")

        monkeypatch.setattr(codegraph_recon.subprocess, "run", fake_run)
        out = codegraph_recon.build_and_ingest(clone, "o/r")
        assert out["status"] == "skipped"
        assert "ingest_graph failed" in out["reason"]
        # Clone kept clean even on the ingest-failure path.
        assert not (clone / "graphify-out").exists()

    def test_skip_when_ingest_bridge_emits_unparseable_json(self, monkeypatch, tmp_path):
        """ingest bridge returns rc=0 but non-JSON stdout -> skipped with the
        no-parseable-summary reason (never raises). Covers the JSON-parse branch."""
        monkeypatch.delenv(codegraph_recon._CODEGRAPH_ENV, raising=False)
        monkeypatch.setattr(codegraph_recon.shutil, "which", lambda n: "/usr/bin/graphify")
        memex_root = tmp_path / "memex"
        memex_root.mkdir()
        monkeypatch.setattr(codegraph_recon, "find_memex_root", lambda: memex_root)
        clone = tmp_path / "clone"
        clone.mkdir()
        graph_json = clone / "graphify-out" / "graph.json"

        def fake_run(argv, **k):
            if argv[0] == "graphify":
                graph_json.parent.mkdir(parents=True, exist_ok=True)
                graph_json.write_text("{}")
                return CompletedProcess(args=argv, returncode=0, stdout="", stderr="")
            return CompletedProcess(args=argv, returncode=0, stdout="not json at all", stderr="")

        monkeypatch.setattr(codegraph_recon.subprocess, "run", fake_run)
        out = codegraph_recon.build_and_ingest(clone, "o/r")
        assert out["status"] == "skipped"
        assert "no parseable JSON summary" in out["reason"]


# ── Query helpers ──────────────────────────────────────────────────────────


class TestQueryHelpers:
    def test_where_is_passes_args_and_parses_json(self, monkeypatch, tmp_path):
        monkeypatch.setattr(codegraph_recon, "find_memex_root", lambda: tmp_path)
        seen: list[tuple[list, dict]] = []

        def fake_run(argv, **k):
            seen.append((argv, k))
            return CompletedProcess(
                args=argv, returncode=0, stdout='[{"file":"a.py","line":3}]', stderr=""
            )

        monkeypatch.setattr(codegraph_recon.subprocess, "run", fake_run)
        result = codegraph_recon.where_is("o/r", "myfn")
        assert result == [{"file": "a.py", "line": 3}]
        argv, kw = seen[0]
        assert argv[0] == sys.executable
        assert argv[1] == "-c"
        assert "where_is" in argv
        assert "o/r" in argv
        assert "myfn" in argv
        # REGRESSION (bridge cwd): the query bridge MUST run with cwd == memex_root
        # so `from scripts import code_graph` resolves to memex, not the caller's
        # scripts/ package (same shadowing bug as the ingest bridge).
        assert kw["cwd"] == str(tmp_path)
        assert kw["env"]["PYTHONPATH"] == str(tmp_path)

    def test_query_helper_raises_when_memex_missing(self, monkeypatch):
        monkeypatch.setattr(codegraph_recon, "find_memex_root", lambda: None)
        with pytest.raises(RuntimeError):
            codegraph_recon.callers("o/r", "node-1")


# ── CLI ────────────────────────────────────────────────────────────────────


class TestCli:
    def test_build_emits_json_status_on_skip(self, monkeypatch, tmp_path, capsys):
        # Force a skip via disabled gate; CLI must exit 0 + print JSON status.
        monkeypatch.setenv(codegraph_recon._CODEGRAPH_ENV, "0")
        rc = codegraph_recon.main(["build", str(tmp_path), "o/r"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out.strip())
        assert out["status"] == "skipped"

    def test_build_accepts_git_url(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv(codegraph_recon._CODEGRAPH_ENV, "0")
        rc = codegraph_recon.main(["build", str(tmp_path), "https://github.com/octo/widget.git"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out.strip())
        assert out["status"] == "skipped"

    def test_subprocess_invocation_skips_clean(self, tmp_path):
        # End-to-end CLI via a real subprocess: even with deps absent it must
        # exit 0 and print a JSON status (the skip path), never a traceback.
        repo_root = Path(__file__).resolve().parent.parent
        env = {**os.environ, "PYTHONPATH": ".", "KAIZEN_CODEGRAPH": "0"}
        proc = subprocess.run(
            [sys.executable, "scripts/codegraph_recon.py", "build", str(tmp_path), "o/r"],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        status = json.loads(proc.stdout.strip())
        assert status["status"] == "skipped"

    def test_subprocess_invocation_without_pythonpath(self, tmp_path):
        """REGRESSION: internal/cycle/SKILL.md invokes the CLI as bare
        `python3 scripts/codegraph_recon.py ...` (no PYTHONPATH). The
        plugin_cache import must not break that entrypoint."""
        repo_root = Path(__file__).resolve().parent.parent
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["KAIZEN_CODEGRAPH"] = "0"
        proc = subprocess.run(
            [sys.executable, "scripts/codegraph_recon.py", "build", str(tmp_path), "o/r"],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        status = json.loads(proc.stdout.strip())
        assert status["status"] == "skipped"


# ── Real integration (only when graphify + memex>=2.9.0 are installed) ───────
#
# These exercise the ACTUAL production caller end-to-end — the path the mocked
# tests above cannot reach (real graphify subprocess + real memex code_graph
# import bridge). They are the regression guard for the cwd/PYTHONPATH-shadow
# and graphify-env bugs that only surfaced once the deps were installed. They
# skip cleanly in CI (deps absent), so they never make CI flaky.

_GRAPHIFY = shutil.which("graphify")
_MEMEX = codegraph_recon.find_memex_root()
_DEPS_PRESENT = _GRAPHIFY is not None and _MEMEX is not None


@pytest.mark.skipif(not _DEPS_PRESENT, reason="graphify + memex>=2.9.0 not installed")
class TestRealEndToEnd:
    def test_build_and_query_against_a_real_tree(self, tmp_path, monkeypatch):
        """Real graphify build -> real memex ingest -> real query, run from a cwd
        that has its OWN scripts/ package (reproducing the kaizen-root shadow).
        Isolated MEMEX_HOME so it never touches the user's real code_graph.db."""
        monkeypatch.delenv(codegraph_recon._CODEGRAPH_ENV, raising=False)
        # Isolate the memex store under tmp so we don't write the real one.
        home = tmp_path / "memexhome"
        home.mkdir()
        monkeypatch.setenv("MEMEX_HOME", str(home))
        monkeypatch.setenv("MEMEX_HOME_ALLOW_UNUSUAL", "1")

        # A tiny target with a couple of Python symbols + a call edge.
        target = tmp_path / "target"
        (target / "pkg").mkdir(parents=True)
        (target / "pkg" / "a.py").write_text("def foo():\n    return bar()\n")
        (target / "pkg" / "b.py").write_text("def bar():\n    return 1\n")

        # Decoy scripts/ in cwd to reproduce the shadow: run from a dir that has
        # its own (non-memex) scripts package, exactly like the kaizen root.
        decoy = tmp_path / "decoy"
        (decoy / "scripts").mkdir(parents=True)
        (decoy / "scripts" / "__init__.py").write_text("")
        monkeypatch.chdir(decoy)

        out = codegraph_recon.build_and_ingest(target, "octo/widget", built_at_commit="HEAD")
        assert out["status"] == "ingested", out
        assert out["nodes"] > 0
        # graphify-out must not be left in the target.
        assert not (target / "graphify-out").exists()

        # Real query bridge round-trips (would ImportError if cwd shadow regressed).
        hits = codegraph_recon.where_is("octo/widget", "foo")
        assert any(h.get("label", "").startswith("foo") for h in hits), hits
