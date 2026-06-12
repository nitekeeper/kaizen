"""F16 — mandatory loom-agent-chat inter-agent comms (2026-06-11 user directive).

When the Loom desktop app's agent-chat MCP server is available, EVERY
kaizen team/subagent dispatch MUST carry the loom-comms instruction
block so the spawned agents communicate over loom-agent-chat. This
module is the single implementation point:

  - :func:`find_loom_client` — locate the bundled stdlib-only
    ``loom_chat.py`` client (env pin → plugin glob → sibling app).
  - :func:`detect_loom` — cached availability probe. The DEFAULT is
    auto-detect (mandatory when available); ``KAIZEN_LOOM_COMMS=0`` is
    the ONLY opt-out.
  - :func:`loom_comms_block` — the MANDATORY instruction block injected
    into agent prompts.
  - :func:`augment_dispatch` — splice the block into a teammate-bound
    dispatch body immediately BEFORE the F7 reply-rule trailer
    (mirrors ``dispatch_templates._inject_terse_before_trailer`` so the
    trailer stays terminal). Bodies WITHOUT the trailer (e.g. the GAP-7
    ``shutdown_request`` STRUCTURED-JSON payload) pass through
    unchanged — protocol messages must never grow prose.
  - :func:`team_lead_setup` / :func:`team_lead_teardown` — best-effort
    team-lead register + channel-create / deregister for the team-mode
    executor.
  - :func:`channel_for_run` — the SINGLE channel-naming authority for
    both modes (exposed to subagent mode via the ``channel`` CLI).

Degradation contract (F16): loom failures degrade gracefully and never
abort a cycle. Every subprocess call here is timeout-bounded and every
public function is non-raising on loom errors.

CLI (used by ``internal/cycle/SKILL.md`` in subagent mode)::

    python3 scripts/loom_comms.py detect
    python3 scripts/loom_comms.py channel --run-id <run_id> --cycle <cycle_n>
    python3 scripts/loom_comms.py block --role <role> --channel <chan>

Stdlib-only by design (kaizen runtime has no third-party deps).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

_log = logging.getLogger(__name__)

# Probe budget — loom is a local server; anything slower than this is
# effectively down. Mirrors the design's ~5s detect bound.
DETECT_TIMEOUT_S = 5
# register/create-channel are two round-trips; give them a bit more.
_SETUP_TIMEOUT_S = 10

# Bounded-depth glob under ~/.claude/plugins — the loom-agent-chat
# plugin may sit at varying nesting depending on the marketplace layout
# (e.g. ``plugins/<name>/`` or ``plugins/<marketplace>/<name>/<ver>/``).
_PLUGIN_GLOB_PATTERNS = (
    "*/skills/loom-chat/loom_chat.py",
    "*/*/skills/loom-chat/loom_chat.py",
    "*/*/*/skills/loom-chat/loom_chat.py",
)
_SIBLING_FALLBACK = Path("~/apps/loom-agent-chat/skills/loom-chat/loom_chat.py")

# Loom channel names: keep them slug-shaped and bounded. 64 is comfortably
# under any practical server limit and keeps tmux/UI rendering sane.
_MAX_CHANNEL_LEN = 64

# Sentinel substring used for idempotence: a body already carrying the
# loom block is returned unchanged by augment_dispatch (exactly-once).
_BLOCK_MARKER = "Loom comms (rule F16, MANDATORY)"

# Per-process detect cache — one probe per orchestrator/executor run.
_detect_cache: dict | None = None


def reset_cache() -> None:
    """Clear the per-process detect cache (tests + long-lived sessions)."""
    global _detect_cache
    _detect_cache = None


def find_loom_client() -> str | None:
    """Locate ``loom_chat.py``. Precedence:

    1. ``KAIZEN_LOOM_CHAT`` env — an EXPLICIT path pin. When set, it is
       authoritative: if the pinned file is missing we return ``None``
       rather than silently falling through to a different client.
    2. Bounded-depth glob under ``~/.claude/plugins/`` for
       ``**/skills/loom-chat/loom_chat.py``.
    3. Sibling-app fallback ``~/apps/loom-agent-chat/skills/loom-chat/loom_chat.py``.
    """
    env_pin = os.environ.get("KAIZEN_LOOM_CHAT")
    if env_pin:
        pinned = Path(env_pin).expanduser()
        return str(pinned) if pinned.is_file() else None
    plugins_root = Path.home() / ".claude" / "plugins"
    for pattern in _PLUGIN_GLOB_PATTERNS:
        hits = sorted(plugins_root.glob(pattern))
        if hits:
            return str(hits[0])
    sibling = _SIBLING_FALLBACK.expanduser()
    if sibling.is_file():
        return str(sibling)
    return None


def _detect_uncached() -> dict:
    """One real availability probe. Shape on success::

        {"available": True, "url": ..., "port": ..., "source": ..., "client": <path>}

    On any failure ``{"available": False, "reason"|"source": ...}``.
    """
    # Explicit kill-switch — the ONLY opt-out. Default is auto-detect:
    # loom comms are MANDATORY whenever the probe succeeds.
    if os.environ.get("KAIZEN_LOOM_COMMS") == "0":
        return {"available": False, "source": "disabled"}
    client = find_loom_client()
    if client is None:
        return {"available": False, "reason": "client_not_found"}
    try:
        proc = subprocess.run(
            [sys.executable, client, "detect"],
            capture_output=True,
            encoding="utf-8",
            timeout=DETECT_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"available": False, "reason": f"detect_failed: {exc}", "client": client}
    try:
        payload = json.loads(proc.stdout.strip() or "{}")
    except ValueError:
        return {"available": False, "reason": "detect_output_not_json", "client": client}
    if not isinstance(payload, dict):
        return {"available": False, "reason": "detect_output_not_object", "client": client}
    if proc.returncode != 0 or not payload.get("available"):
        reason = payload.get("error") or f"detect_exit_{proc.returncode}"
        return {"available": False, "reason": str(reason), "client": client}
    result = dict(payload)
    result["client"] = client
    return result


def detect_loom() -> dict:
    """Cached :func:`_detect_uncached` — one probe per process."""
    global _detect_cache
    if _detect_cache is None:
        _detect_cache = _detect_uncached()
    return _detect_cache


def channel_for_team(team_name: str) -> str:
    """Derive the loom channel name from the team/run identity.

    Slugified (lowercase, ``[a-z0-9-]`` only), guaranteed ``kaizen``
    prefix, bounded to a loom-safe length.
    """
    slug = re.sub(r"[^a-z0-9-]+", "-", team_name.lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    if not slug.startswith("kaizen"):
        slug = f"kaizen-{slug}" if slug else "kaizen"
    return slug[:_MAX_CHANNEL_LEN].rstrip("-")


def channel_for_run(run_id: int, cycle_n: int) -> str:
    """SINGLE channel-naming authority for BOTH modes.

    The team-mode executor names its team ``kaizen-cycle-<run_id>-<cycle_n>``
    and derives the channel from it via :func:`channel_for_team`; the
    subagent-mode orchestrator obtains the SAME name via the ``channel``
    CLI subcommand below. One convention everywhere — never compose a
    channel name by hand in a SKILL or prompt.
    """
    return channel_for_team(f"kaizen-cycle-{run_id}-{cycle_n}")


def loom_comms_block(role: str, channel: str, client_path: str) -> str:
    """The MANDATORY loom-comms instruction block for an agent prompt.

    Injected by :func:`augment_dispatch` (team mode) and embedded by the
    subagent-mode orchestrator per ``internal/cycle/SKILL.md``. The F7
    caveat at the end is load-bearing: loom carries peer chatter; the
    ``SendMessage(to="team-lead", ...)`` completion reply stays the ONLY
    completion signal in team mode.
    """
    return (
        f"IMPORTANT — Loom comms (rule F16, MANDATORY): Loom agent-chat is "
        f"AVAILABLE on this host and its use is REQUIRED for inter-agent "
        f"communication.\n"
        f'- Register once with your bare role id: `python3 {client_path} register "{role}"` '
        f"— capture the returned `assigned_name` and use it verbatim as "
        f'`--as "<assigned>"` in every later command.\n'
        f'- Join the cycle channel: `python3 {client_path} join {channel} --as "<assigned>"`.\n'
        f"- Peer names may be collision-suffixed (e.g. `backend-engineer-1-2`): "
        f"before sending, discover each peer's ACTUAL assigned name from the "
        f"channel member list — the `members` array in the join response, or "
        f'`python3 {client_path} list-channels --as "<assigned>"` — and address '
        f"that exact name.\n"
        f"- Send ALL teammate-to-teammate / peer communication (status updates, "
        f"clarifications, conflict negotiation, findings summaries) via: "
        f'`python3 {client_path} send {channel} "<peer>" "<msg>" --as "<assigned>"`.\n'
        f"- Check your inbox at EACH phase boundary BEFORE acting: "
        f'`python3 {client_path} inbox --as "<assigned>"` then '
        f'`python3 {client_path} read {channel} --as "<assigned>"`.\n'
        f"- Message bodies must be ≤500 chars. For longer content, write a file "
        f"under `.loom/temp/` (relative to the root of the repo/clone you are "
        f"working in; create it if absent) and send a short pointer message with "
        f"the file's absolute path + a 1-2 sentence summary.\n"
        f'- Deregister when your task completes: `python3 {client_path} deregister --as "<assigned>"`.\n'
        f"- Loom failures must NEVER block your task — on any loom error, note it "
        f"and continue working.\n"
        f"CRITICAL CAVEAT: in team mode the "
        f'SendMessage(to="team-lead", ...) completion reply (rule F7) remains '
        f"REQUIRED and is the ONLY completion signal — loom does NOT replace it. "
        f"Loom carries everything else."
    )


def augment_dispatch(message: str, *, role: str, channel: str) -> str:
    """Splice the loom-comms block into a teammate-bound dispatch body.

    - Loom unavailable (or any error) → ``message`` unchanged.
    - F7 trailer (``dispatch_templates.TEAMMATE_REPLY_RULE``) present →
      insert the block immediately BEFORE the trailer (mirrors
      ``_inject_terse_before_trailer``: the trailer stays the prompt's
      LAST instruction).
    - No trailer → unchanged. This is the protocol-payload guard: GAP-7
      ``shutdown_request`` JSON bodies (and any other control payloads)
      must pass through byte-exact.
    - Block already present → unchanged (exactly-once).

    Never raises — F16's degradation contract says loom must never
    abort a cycle, and this function sits on the hot dispatch path.
    """
    try:
        info = detect_loom()
        if not info.get("available"):
            return message
        if _BLOCK_MARKER in message:
            return message
        # Imported lazily: the CLI entrypoint below must work without
        # PYTHONPATH gymnastics, and only this function needs the trailer.
        from scripts.dispatch_templates import TEAMMATE_REPLY_RULE

        trailer = TEAMMATE_REPLY_RULE.strip()
        idx = message.rfind(trailer)
        if idx == -1:
            return message
        block = loom_comms_block(role=role, channel=channel, client_path=info["client"])
        head = message[:idx].rstrip()
        return f"{head}\n\n{block}\n\n{message[idx:]}"
    except Exception as exc:  # pragma: no cover — defensive (F16: never block)
        _log.warning("kaizen F16: augment_dispatch failed (%s) — dispatching unaugmented", exc)
        return message


def team_lead_setup(client_path: str, channel: str, *, name: str = "team-lead") -> str | None:
    """Best-effort team-lead register + channel create (team mode).

    Returns the team-lead's ASSIGNED loom name (the server may
    collision-suffix, e.g. ``team-lead-2``) when register and
    create/join succeeded; ``None`` on any failure. The caller passes
    the assigned name to :func:`team_lead_teardown` at cycle end.
    Never raises — loom errors never fail the cycle.
    """
    try:
        reg = subprocess.run(
            [sys.executable, client_path, "register", name],
            capture_output=True,
            encoding="utf-8",
            timeout=_SETUP_TIMEOUT_S,
        )
        if reg.returncode != 0:
            _log.warning("kaizen F16: loom register %r failed: %s", name, reg.stdout.strip())
            return None
        assigned = name
        with contextlib.suppress(ValueError):
            assigned = json.loads(reg.stdout.strip()).get("assigned_name") or name
        create = subprocess.run(
            [sys.executable, client_path, "create-channel", channel, "--as", assigned],
            capture_output=True,
            encoding="utf-8",
            timeout=_SETUP_TIMEOUT_S,
        )
        if create.returncode != 0:
            # Channel may already exist — join is the idempotent fallback.
            join = subprocess.run(
                [sys.executable, client_path, "join", channel, "--as", assigned],
                capture_output=True,
                encoding="utf-8",
                timeout=_SETUP_TIMEOUT_S,
            )
            if join.returncode != 0:
                _log.warning(
                    "kaizen F16: loom create/join channel %r failed: %s",
                    channel,
                    join.stdout.strip(),
                )
                return None
        return assigned
    except Exception as exc:  # pragma: no cover — defensive (F16: never block)
        _log.warning("kaizen F16: loom team-lead setup failed: %s", exc)
        return None


def team_lead_teardown(client_path: str, assigned: str) -> bool:
    """Best-effort team-lead deregister (team mode cycle teardown).

    ``assigned`` is the name :func:`team_lead_setup` returned (it may be
    collision-suffixed). Returns True on success; False on any failure.
    Never raises — loom errors never fail the cycle.
    """
    try:
        proc = subprocess.run(
            [sys.executable, client_path, "deregister", "--as", assigned],
            capture_output=True,
            encoding="utf-8",
            timeout=_SETUP_TIMEOUT_S,
        )
        if proc.returncode != 0:
            _log.warning("kaizen F16: loom deregister %r failed: %s", assigned, proc.stdout.strip())
            return False
        return True
    except Exception as exc:  # pragma: no cover — defensive (F16: never block)
        _log.warning("kaizen F16: loom team-lead teardown failed: %s", exc)
        return False


def _main(argv: list[str] | None = None) -> int:
    """CLI for the subagent-mode orchestrator (internal/cycle/SKILL.md).

    ``detect`` prints the detect JSON (exit 0 available / 3 not).
    ``channel --run-id X --cycle N`` prints the canonical channel name
    (the single naming authority — same name team mode derives).
    ``block --role R --channel C`` prints the instruction block to embed
    in a dispatched Agent prompt (exit 3 when loom is unavailable).
    """
    parser = argparse.ArgumentParser(prog="loom_comms", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("detect", help="print loom availability JSON")
    chan_p = sub.add_parser("channel", help="print the canonical cycle channel name")
    chan_p.add_argument("--run-id", required=True, type=int)
    chan_p.add_argument("--cycle", required=True, type=int)
    block_p = sub.add_parser("block", help="print the F16 loom-comms prompt block")
    block_p.add_argument("--role", required=True)
    block_p.add_argument("--channel", required=True)
    args = parser.parse_args(argv)

    if args.cmd == "channel":
        # Pure derivation — no loom probe needed (and must work even when
        # loom is down so the orchestrator can log a stable channel name).
        print(channel_for_run(args.run_id, args.cycle))
        return 0

    info = detect_loom()
    if args.cmd == "detect":
        print(json.dumps(info))
        return 0 if info.get("available") else 3
    # block
    if not info.get("available"):
        print(json.dumps({"error": "loom unavailable", **info}))
        return 3
    print(loom_comms_block(role=args.role, channel=args.channel, client_path=info["client"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
