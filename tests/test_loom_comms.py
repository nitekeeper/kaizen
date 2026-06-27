"""Tests for scripts/loom_comms.py — F16 mandatory loom-agent-chat comms.

Coverage layers (per feedback-test-the-production-caller-not-just-units):

  1. Units — find_loom_client precedence, detect_loom kill-switch /
     failure paths / caching, loom_comms_block content, augment_dispatch
     splice/no-op behaviour, channel slugging, team_lead_setup.
  2. Guarded REAL integration — full register→channel→send→inbox→
     deregister roundtrip against a live Loom (KAIZEN_LOOM_LIVE=1 only;
     auto-skips in CI).

The autouse `_isolate_loom_comms` fixture in conftest.py sets
KAIZEN_LOOM_COMMS=0 + cold cache for every test; loom-exercising tests
override the env and reset the cache themselves.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.loom_comms as loom_comms
from scripts.dispatch_templates import (
    TEAMMATE_REPLY_RULE,
    phase_2_preanalysis,
    phase_5d_shutdown,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

_TRAILER = TEAMMATE_REPLY_RULE.strip()
_MARKER = loom_comms._BLOCK_MARKER

# ── fake loom_chat.py clients ─────────────────────────────────────────────

_FAKE_OK_CLIENT = """\
import json, sys
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
counter = {counter_path!r}
if counter:
    with open(counter, "a") as f:
        f.write(cmd + "\\n")
if cmd == "detect":
    print(json.dumps({{"available": True, "url": "http://127.0.0.1:7077/mcp",
                       "port": 7077, "source": "scan"}}))
    sys.exit(0)
if cmd == "register":
    print(json.dumps({{"assigned_name": sys.argv[2], "session_id": "s", "url": "u"}}))
    sys.exit(0)
if cmd == "create-channel":
    sys.exit({create_channel_rc})
if cmd == "join":
    sys.exit({join_rc})
sys.exit(0)
"""


def _write_fake_client(
    tmp_path: Path,
    *,
    counter: Path | None = None,
    create_channel_rc: int = 0,
    join_rc: int = 0,
) -> Path:
    client = tmp_path / "loom_chat.py"
    client.write_text(
        _FAKE_OK_CLIENT.format(
            counter_path=str(counter) if counter else "",
            create_channel_rc=create_channel_rc,
            join_rc=join_rc,
        )
    )
    return client


def _enable_loom(monkeypatch, client: Path) -> None:
    """Lift the autouse kill-switch and pin the fake client."""
    monkeypatch.delenv("KAIZEN_LOOM_COMMS", raising=False)
    monkeypatch.setenv("KAIZEN_LOOM_CHAT", str(client))
    loom_comms.reset_cache()


# ── find_loom_client precedence ───────────────────────────────────────────


class TestFindLoomClient:
    def _plugin_client(self, home: Path, depth_parts: tuple[str, ...]) -> Path:
        d = home / ".claude" / "plugins"
        for part in depth_parts:
            d = d / part
        d = d / "skills" / "loom-chat"
        d.mkdir(parents=True)
        client = d / "loom_chat.py"
        client.write_text("# stub\n")
        return client

    def test_env_pin_wins_over_plugin_glob_and_sibling(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        plugin = self._plugin_client(home, ("loom-agent-chat",))
        sibling = home / "apps" / "loom-agent-chat" / "skills" / "loom-chat" / "loom_chat.py"
        sibling.parent.mkdir(parents=True)
        sibling.write_text("# stub\n")
        pinned = tmp_path / "pinned_loom_chat.py"
        pinned.write_text("# stub\n")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("KAIZEN_LOOM_CHAT", str(pinned))
        assert loom_comms.find_loom_client() == str(pinned)
        assert str(plugin) != str(pinned)  # the glob candidate really existed

    def test_env_pin_to_missing_file_is_authoritative_none(self, tmp_path, monkeypatch):
        """An explicit pin to a missing path does NOT fall through."""
        home = tmp_path / "home"
        self._plugin_client(home, ("loom-agent-chat",))
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("KAIZEN_LOOM_CHAT", str(tmp_path / "nope.py"))
        assert loom_comms.find_loom_client() is None

    @pytest.mark.parametrize(
        "depth_parts",
        [("loom-agent-chat",), ("market", "loom-agent-chat"), ("m", "loom", "1.0.0")],
    )
    def test_plugin_glob_bounded_depths(self, tmp_path, monkeypatch, depth_parts):
        home = tmp_path / "home"
        client = self._plugin_client(home, depth_parts)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("KAIZEN_LOOM_CHAT", raising=False)
        assert loom_comms.find_loom_client() == str(client)

    def test_sibling_fallback_when_no_plugin(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        sibling = home / "apps" / "loom-agent-chat" / "skills" / "loom-chat" / "loom_chat.py"
        sibling.parent.mkdir(parents=True)
        sibling.write_text("# stub\n")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("KAIZEN_LOOM_CHAT", raising=False)
        assert loom_comms.find_loom_client() == str(sibling)

    def test_nothing_found_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "empty_home"))
        monkeypatch.delenv("KAIZEN_LOOM_CHAT", raising=False)
        assert loom_comms.find_loom_client() is None


# ── detect_loom ───────────────────────────────────────────────────────────


class TestDetectLoom:
    def test_kill_switch_short_circuits(self):
        # conftest autouse fixture already set KAIZEN_LOOM_COMMS=0.
        assert loom_comms.detect_loom() == {"available": False, "source": "disabled"}

    def test_available_includes_client_path(self, tmp_path, monkeypatch):
        client = _write_fake_client(tmp_path)
        _enable_loom(monkeypatch, client)
        info = loom_comms.detect_loom()
        assert info["available"] is True
        assert info["client"] == str(client)
        assert info["port"] == 7077

    def test_client_not_found(self, tmp_path, monkeypatch):
        monkeypatch.delenv("KAIZEN_LOOM_COMMS", raising=False)
        monkeypatch.delenv("KAIZEN_LOOM_CHAT", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path / "empty_home"))
        loom_comms.reset_cache()
        info = loom_comms.detect_loom()
        assert info == {"available": False, "reason": "client_not_found"}

    def test_garbage_json_is_unavailable(self, tmp_path, monkeypatch):
        client = tmp_path / "loom_chat.py"
        client.write_text("print('this is not json')\n")
        _enable_loom(monkeypatch, client)
        info = loom_comms.detect_loom()
        assert info["available"] is False
        assert info["reason"] == "detect_output_not_json"

    def test_unavailable_exit_3(self, tmp_path, monkeypatch):
        client = tmp_path / "loom_chat.py"
        client.write_text('import json,sys;print(json.dumps({"available": False}));sys.exit(3)\n')
        _enable_loom(monkeypatch, client)
        info = loom_comms.detect_loom()
        assert info["available"] is False
        assert info["reason"] == "detect_exit_3"

    def test_timeout_is_unavailable(self, tmp_path, monkeypatch):
        client = tmp_path / "loom_chat.py"
        client.write_text("import time; time.sleep(5)\n")
        _enable_loom(monkeypatch, client)
        monkeypatch.setattr(loom_comms, "DETECT_TIMEOUT_S", 0.2)
        info = loom_comms.detect_loom()
        assert info["available"] is False
        assert info["reason"].startswith("detect_failed")

    def test_result_is_cached_until_reset(self, tmp_path, monkeypatch):
        counter = tmp_path / "calls.log"
        client = _write_fake_client(tmp_path, counter=counter)
        _enable_loom(monkeypatch, client)
        loom_comms.detect_loom()
        loom_comms.detect_loom()
        assert counter.read_text().count("detect") == 1
        loom_comms.reset_cache()
        loom_comms.detect_loom()
        assert counter.read_text().count("detect") == 2


# ── channel_for_team ──────────────────────────────────────────────────────


class TestChannelForTeam:
    def test_team_name_already_kaizen_prefixed_passes_through(self):
        assert loom_comms.channel_for_team("kaizen-cycle-1-1") == "kaizen-cycle-1-1"

    def test_slugifies_and_prefixes(self):
        assert loom_comms.channel_for_team("My Team!!") == "kaizen-my-team"

    def test_bounded_length(self):
        chan = loom_comms.channel_for_team("kaizen-" + "x" * 200)
        assert len(chan) <= loom_comms._MAX_CHANNEL_LEN
        assert not chan.endswith("-")

    def test_channel_for_run_matches_team_mode_derivation(self):
        """Single naming authority: subagent mode (channel_for_run / the
        `channel` CLI) must yield the EXACT name team mode derives from
        its team name."""
        run_id, cycle_n = 7, 2
        team_mode_name = loom_comms.channel_for_team(f"kaizen-cycle-{run_id}-{cycle_n}")
        assert loom_comms.channel_for_run(run_id, cycle_n) == team_mode_name == "kaizen-cycle-7-2"


# ── loom_comms_block content ──────────────────────────────────────────────


class TestLoomCommsBlock:
    def test_block_mentions_full_protocol(self):
        block = loom_comms.loom_comms_block(
            role="backend-engineer-1", channel="kaizen-cycle-9-1", client_path="/p/loom_chat.py"
        )
        # Mandatory language + rule id
        assert "F16" in block
        assert "MANDATORY" in block
        assert "REQUIRED" in block
        # Full command surface, role + channel + client substituted
        assert 'register "backend-engineer-1"' in block
        assert "python3 /p/loom_chat.py" in block
        assert "join kaizen-cycle-9-1" in block
        assert 'send kaizen-cycle-9-1 "<peer>"' in block
        assert "inbox" in block
        assert "read kaizen-cycle-9-1" in block
        assert "deregister" in block
        # Peer-name discovery: assigned names may be collision-suffixed,
        # so the block must direct agents to the channel member list.
        assert "list-channels" in block
        assert "ACTUAL assigned name" in block
        assert "collision-suffixed" in block
        # Body-length + long-content pointer rule, with an explicit cwd
        # anchor for the relative pointer-file path.
        assert "500" in block
        assert ".loom/temp/" in block
        assert "root of the repo/clone" in block
        # The load-bearing F7 caveat
        assert 'SendMessage(to="team-lead", ...)' in block
        assert "ONLY completion signal" in block
        # Degradation contract
        assert "NEVER block" in block


# ── augment_dispatch ──────────────────────────────────────────────────────


@pytest.fixture
def loom_available():
    """Inject an 'available' detect result without touching subprocess."""
    loom_comms._detect_cache = {
        "available": True,
        "url": "http://127.0.0.1:7077/mcp",
        "client": "/fake/loom_chat.py",
    }
    yield
    loom_comms.reset_cache()


def _rendered_phase_2() -> str:
    return phase_2_preanalysis(agenda_items=["improve the docs"], participant="backend-engineer-1")


class TestAugmentDispatch:
    def test_unavailable_returns_message_unchanged(self):
        msg = _rendered_phase_2()
        out = loom_comms.augment_dispatch(msg, role="backend-engineer-1", channel="kaizen-c")
        assert out == msg

    def test_injects_block_immediately_before_trailer(self, loom_available):
        msg = _rendered_phase_2()
        out = loom_comms.augment_dispatch(msg, role="backend-engineer-1", channel="kaizen-c")
        assert out != msg
        assert _MARKER in out
        assert out.endswith(_TRAILER), "F7 trailer must remain the terminal paragraph"
        assert out.index(_MARKER) < out.rindex(_TRAILER)

    def test_no_trailer_payload_passes_through_byte_exact(self, loom_available):
        """GAP-7 shutdown JSON (and any control payload) must not grow prose."""
        payload = phase_5d_shutdown(request_id="11111111-2222-3333-4444-555555555555")
        out = loom_comms.augment_dispatch(payload, role="pm-1", channel="kaizen-c")
        assert out == payload
        json.loads(out)  # still valid JSON

    def test_exactly_once_idempotent(self, loom_available):
        msg = _rendered_phase_2()
        once = loom_comms.augment_dispatch(msg, role="be-1", channel="kaizen-c")
        twice = loom_comms.augment_dispatch(once, role="be-1", channel="kaizen-c")
        assert twice == once
        assert once.count(_MARKER) == 1

    def test_exact_count_and_ordering_of_all_mechanisms(self, loom_available):
        """Exact-count rule (feedback-multi-mechanism-test-exact-count):
        phase body + loom block + F7 trailer — each exactly once, in that
        order."""
        out = loom_comms.augment_dispatch(
            _rendered_phase_2(), role="backend-engineer-1", channel="kaizen-c"
        )
        assert out.count(_MARKER) == 1
        assert out.count(_TRAILER) == 1
        body_idx = out.index("backend-engineer-1")  # phase body opener mentions participant
        assert body_idx < out.index(_MARKER) < out.index(_TRAILER)
        assert out.endswith(_TRAILER)


# ── team_lead_setup ───────────────────────────────────────────────────────


class TestTeamLeadSetup:
    def test_register_and_create_succeed_returns_assigned_name(self, tmp_path):
        counter = tmp_path / "calls.log"
        client = _write_fake_client(tmp_path, counter=counter)
        assert loom_comms.team_lead_setup(str(client), "kaizen-c") == "team-lead"
        calls = counter.read_text().splitlines()
        assert calls == ["register", "create-channel"]

    def test_collision_suffixed_assigned_name_is_returned(self, tmp_path):
        """The server may suffix on collision; the ASSIGNED name (not the
        requested one) must flow back so teardown deregisters the right
        session."""
        client = tmp_path / "loom_chat.py"
        client.write_text(
            "import json, sys\n"
            "cmd = sys.argv[1]\n"
            "if cmd == 'register':\n"
            "    print(json.dumps({'assigned_name': sys.argv[2] + '-2'}))\n"
            "sys.exit(0)\n"
        )
        assert loom_comms.team_lead_setup(str(client), "kaizen-c") == "team-lead-2"

    def test_create_fails_join_fallback_succeeds(self, tmp_path):
        counter = tmp_path / "calls.log"
        client = _write_fake_client(tmp_path, counter=counter, create_channel_rc=4)
        assert loom_comms.team_lead_setup(str(client), "kaizen-c") == "team-lead"
        assert counter.read_text().splitlines() == ["register", "create-channel", "join"]

    def test_create_and_join_fail_returns_none(self, tmp_path):
        client = _write_fake_client(tmp_path, create_channel_rc=4, join_rc=4)
        assert loom_comms.team_lead_setup(str(client), "kaizen-c") is None

    def test_register_failure_returns_none(self, tmp_path):
        client = tmp_path / "loom_chat.py"
        client.write_text("import sys; sys.exit(4)\n")
        assert loom_comms.team_lead_setup(str(client), "kaizen-c") is None

    def test_never_raises_on_missing_client(self):
        assert loom_comms.team_lead_setup("/nonexistent/loom_chat.py", "kaizen-c") is None


class TestTeamLeadTeardown:
    def test_deregister_succeeds(self, tmp_path):
        counter = tmp_path / "calls.log"
        client = _write_fake_client(tmp_path, counter=counter)
        assert loom_comms.team_lead_teardown(str(client), "team-lead-2") is True
        assert counter.read_text().splitlines() == ["deregister"]

    def test_deregister_failure_returns_false(self, tmp_path):
        client = tmp_path / "loom_chat.py"
        client.write_text("import sys; sys.exit(4)\n")
        assert loom_comms.team_lead_teardown(str(client), "team-lead") is False

    def test_never_raises_on_missing_client(self):
        assert loom_comms.team_lead_teardown("/nonexistent/loom_chat.py", "team-lead") is False


# ── CLI ───────────────────────────────────────────────────────────────────


class TestCli:
    def _run_cli(self, args: list[str], env_extra: dict[str, str]) -> subprocess.CompletedProcess:
        env = {**os.environ, **env_extra}
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "loom_comms.py"), *args],
            capture_output=True,
            encoding="utf-8",
            env=env,
            cwd=REPO_ROOT,
            timeout=30,
        )

    def test_detect_kill_switch_exits_3(self):
        proc = self._run_cli(["detect"], {"KAIZEN_LOOM_COMMS": "0"})
        assert proc.returncode == 3
        assert json.loads(proc.stdout) == {"available": False, "source": "disabled"}

    def test_block_prints_block_when_available(self, tmp_path):
        client = _write_fake_client(tmp_path)
        proc = self._run_cli(
            ["block", "--role", "sdet-1", "--channel", "kaizen-x"],
            {"KAIZEN_LOOM_COMMS": "1", "KAIZEN_LOOM_CHAT": str(client)},
        )
        assert proc.returncode == 0
        assert _MARKER in proc.stdout
        assert 'register "sdet-1"' in proc.stdout
        assert "kaizen-x" in proc.stdout

    def test_channel_prints_canonical_name_without_probing(self):
        """`channel` is pure derivation — works even with loom disabled."""
        proc = self._run_cli(
            ["channel", "--run-id", "7", "--cycle", "2"],
            {"KAIZEN_LOOM_COMMS": "0"},
        )
        assert proc.returncode == 0
        assert proc.stdout.strip() == loom_comms.channel_for_run(7, 2)

    def test_block_unavailable_exits_3(self):
        proc = self._run_cli(
            ["block", "--role", "sdet-1", "--channel", "kaizen-x"],
            {"KAIZEN_LOOM_COMMS": "0"},
        )
        assert proc.returncode == 3
        assert json.loads(proc.stdout)["error"] == "loom unavailable"


# ── Guarded REAL-integration roundtrip ────────────────────────────────────


@pytest.mark.skipif(
    os.environ.get("KAIZEN_LOOM_LIVE") != "1",
    reason="live Loom roundtrip runs only with KAIZEN_LOOM_LIVE=1",
)
class TestLiveLoomRoundtrip:
    def _client(self, client: str, *args: str) -> dict:
        proc = subprocess.run(
            [sys.executable, client, *args],
            capture_output=True,
            encoding="utf-8",
            timeout=30,
        )
        assert proc.returncode == 0, f"loom_chat {args} failed: {proc.stdout} {proc.stderr}"
        return json.loads(proc.stdout)

    def test_register_channel_send_inbox_deregister(self, monkeypatch):
        monkeypatch.delenv("KAIZEN_LOOM_COMMS", raising=False)
        loom_comms.reset_cache()
        info = loom_comms.detect_loom()
        if not info.get("available"):
            pytest.skip(f"KAIZEN_LOOM_LIVE=1 but loom not detected: {info}")
        client = info["client"]
        channel = "kaizen-f16-live-test"

        sender = self._client(client, "register", "sdet-1")["assigned_name"]
        receiver = self._client(client, "register", "security-engineer-1")["assigned_name"]
        try:
            # create-channel auto-joins; tolerate pre-existing channel.
            proc = subprocess.run(
                [sys.executable, client, "create-channel", channel, "--as", sender],
                capture_output=True,
                encoding="utf-8",
                timeout=30,
            )
            if proc.returncode != 0:
                self._client(client, "join", channel, "--as", sender)
            self._client(client, "join", channel, "--as", receiver)

            sent = self._client(
                client, "send", channel, receiver, "F16 live roundtrip ping", "--as", sender
            )
            assert receiver in sent.get("recipients", [])

            inbox = self._client(client, "inbox", "--as", receiver)
            assert inbox.get("unread", 0) >= 1

            read = self._client(client, "read", channel, "--as", receiver)
            assert "F16 live roundtrip ping" in json.dumps(read)
        finally:
            self._client(client, "deregister", "--as", sender)
            self._client(client, "deregister", "--as", receiver)
