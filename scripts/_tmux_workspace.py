"""Post-spawn tmux workspace hooks for kaizen team agent mode.

After kaizen spawns N teammates into a tmux workspace, the panes are
laid out in tmux's default tiled mode and every pane is anonymously
titled ``general-purpose`` (Claude Code's default subagent_type). The
hooks in this module reshape the workspace into:

  - one main pane on the LEFT (the lead architect / PM)
  - a 2-column grid on the RIGHT containing the remaining teammates

and rename each teammate's pane title to ``[w{wave_n}] {agent}`` so a
glance at the tmux window shows which wave each agent is participating in.

# Mapping panes to agents (positional)

Claude Code titles every team-mode pane ``general-purpose``, so there is
no in-band way to tell which pane belongs to which teammate. We rely on
the positional convention: ``tmux list-panes`` returns panes in the
order CC's team_create surfaced them, which mirrors the ``members`` list
passed to ``TeamCreate``. The caller passes ``ordered_agents`` matching
that members list and we build the pane_id → agent map by zip.

If CC ever reorders panes the mapping degrades to "titles point at the
wrong agent," which is visible and easy to spot — better than the prior
silent-no-op behavior.

# 2-column grid (programmatic join-pane)

tmux has no built-in "left main + 2-column right" layout. We build it
in two steps:

  1. ``select-layout main-vertical`` → 1 wide left pane, others stacked
     in a single right column.
  2. ``join-pane -h`` to pair the right-column panes: pane2 joins pane1,
     pane4 joins pane3, etc. Each ``-h`` join makes the source a
     horizontal split of the target, producing a row of 2 panes.

Resulting shape (6 teammates, 1 main + 5 right → odd one left alone):

    ┌──────────┬─────┬─────┐
    │          │  a  │  b  │
    │   main   ├─────┼─────┤
    │  (arch)  │  c  │  d  │
    │          ├─────┴─────┤
    │          │     e     │
    └──────────┴───────────┘

The helpers tolerate "no tmux server running" / "workspace missing"
gracefully: tmux exits non-zero with ``no server running on …`` and
we swallow it (a kaizen run must not fail because the user happens to
not have a tmux server up).
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys

_NO_SERVER_HINTS = (
    "no server running",
    "no current client",
    "can't find session",
    "no such session",
)

# kaizen#66 — orchestrator pane identity.
#
# When kaizen runs in a tmux pane (the typical interactive case), tmux sets
# ``TMUX_PANE`` in the orchestrator's environment to the pane_id (e.g. ``%3``).
# That id is stable for the life of the pane, so it is the authoritative
# "this is my own pane" signal. We use it to exclude the orchestrator's own
# pane from ``_list_pane_ids`` (so the zip against the teammate roster does
# not re-classify the PM as a teammate) and to pin the orchestrator's pane
# title once at workspace setup time.
#
# The PM-pane title is a reserved literal so the rest of the codebase never
# mistakes it for a wave/reviewer label. ``KAIZEN_PM_PANE_GLYPH`` lets the
# user fall back to ``*`` or an empty string when the locale lacks UTF-8.

_PM_PANE_GLYPH_DEFAULT = "●"  # U+25CF BLACK CIRCLE ●
_PM_PANE_LABEL_SUFFIX = " team-lead / PM"


def _pm_pane_title() -> str:
    """Return the reserved orchestrator pane title.

    Reads ``KAIZEN_PM_PANE_GLYPH`` once per call (env-only, no flag plumbing)
    so the operator can downgrade to an ASCII glyph or empty string without
    rebuilding kaizen. Default is ``●`` (U+25CF) which renders in every UTF-8
    terminal we test against.
    """
    glyph = os.environ.get("KAIZEN_PM_PANE_GLYPH", _PM_PANE_GLYPH_DEFAULT)
    if not glyph:
        # Empty glyph → drop the leading space too so the title is just
        # ``team-lead / PM``.
        return _PM_PANE_LABEL_SUFFIX.lstrip()
    return f"{glyph}{_PM_PANE_LABEL_SUFFIX}"


def _orchestrator_pane_id() -> str | None:
    """Return the orchestrator's own tmux pane_id, or ``None`` if not in tmux.

    Reads ``$TMUX_PANE`` — set by tmux for any process inside a pane and
    stable for the life of the pane. Returns ``None`` when the env var is
    unset (CI / headless / non-tmux subagent runs) so callers can fall back
    to the older reactive swap-pane path.
    """
    pid = os.environ.get("TMUX_PANE", "").strip()
    return pid or None


# Pane title sanitizer constants (kaizen#61 — Mesh T3 union sanitizer).
#
# strip → escape → left-truncate (64 chars). Pure function, no I/O.
#
# Strip set:
#   - C0 control range (0x00-0x1f, includes ESC \x1b)
#   - DEL (0x7f)
#   - Unicode bidi controls (U+202A-U+202E, U+2066-U+2069) — security-eng's
#     concern: a bidi-injected title can mis-render in the tmux status bar
#     and disguise the active role.
# CSI / OSC initiators are sequences (ESC + bracket / ESC + ]) — once ESC
# itself is stripped, the rest of the sequence becomes inert text that
# stays printable; no separate CSI/OSC handling is required.
# Bidi-control codepoints we strip. Expressed as numeric ``chr()`` so the
# literal codepoints never appear in this source file — bandit B613
# (TrojanSource) would otherwise flag the literals, and more importantly
# THIS file is the sanitizer for bidi-control injection so it must not
# embed them in the first place. The numeric ranges:
#   - U+202A..U+202E: LRE, RLE, PDF, LRO, RLO  (legacy embedding/override)
#   - U+2066..U+2069: LRI, RLI, FSI, PDI       (modern isolates)
_BIDI_CONTROL_CODEPOINTS = tuple(list(range(0x202A, 0x202E + 1)) + list(range(0x2066, 0x2069 + 1)))
_BIDI_CONTROLS = frozenset(chr(cp) for cp in _BIDI_CONTROL_CODEPOINTS)
_STRIP_CHARS = frozenset({chr(i) for i in range(0x20)} | {"\x7f"}) | _BIDI_CONTROLS

# Recognises a leading "[wN] " or "[wNN] " wave prefix so it is preserved
# verbatim by left-truncation (the wave number is the high-information
# bit alongside the role-id tail).
_WAVE_PREFIX_RE = re.compile(r"^(\[w\d+\] )")

_MAX_TITLE_LEN = 64


def _sanitize_title(name: str, max_len: int = _MAX_TITLE_LEN) -> str:
    """Sanitize a pane title in strict order: strip → escape → left-truncate.

    Returns ``"?"`` if the input is empty/None or the result reduces to
    empty after stripping. The function is pure (no I/O) and idempotent
    on already-clean inputs of bounded length.

    Truncation is left-side (keeps the meaningful suffix — role-id tail —
    plus a leading ellipsis). If the title carries a leading ``[wN] ``
    wave prefix, the prefix is preserved verbatim and the truncation eats
    from the middle so a glance at the title still shows the wave number.
    """
    if not name:
        return "?"
    # 1. Strip — drop every control / bidi char in one O(n) pass.
    cleaned = "".join(ch for ch in str(name) if ch not in _STRIP_CHARS)
    if not cleaned:
        return "?"
    # 2. Escape tmux format meta. Single `#` introduces a format spec
    # (e.g. `#H`, `#W`, `#{...}`); doubling it produces a literal `#`.
    cleaned = cleaned.replace("#", "##")
    # 3. Left-truncate. Fast path: already short enough.
    if len(cleaned) <= max_len:
        return cleaned
    # 3a. Wave-prefix preservation. Keep "[wN] " + ellipsis + suffix.
    m = _WAVE_PREFIX_RE.match(cleaned)
    if m and len(m.group(1)) + 2 < max_len:
        prefix = m.group(1)
        # 1 char reserved for the ellipsis.
        budget = max_len - len(prefix) - 1
        return prefix + "…" + cleaned[-budget:]
    # 3b. No prefix (or prefix alone exhausts the budget) — simple
    # left-truncate with a leading ellipsis.
    return "…" + cleaned[-(max_len - 1) :]


def _run_tmux(argv: list[str]) -> subprocess.CompletedProcess:
    """Wrap subprocess.run with tmux-friendly defaults and error tolerance.

    Always uses check=False so the caller can inspect returncode + stderr.
    Captures both streams as text so the no-server signature checks can
    look at stderr.
    """
    return subprocess.run(
        ["tmux", *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _tmux_unavailable(proc: subprocess.CompletedProcess) -> bool:
    """Return True iff ``proc`` indicates "tmux server not running" or similar.

    A nonzero returncode that mentions any of the no-server hints in
    stderr/stdout is treated as a soft failure. The caller returns early
    without raising.
    """
    if proc.returncode == 0:
        return False
    blob = (proc.stderr or "") + (proc.stdout or "")
    blob_lower = blob.lower()
    return any(hint in blob_lower for hint in _NO_SERVER_HINTS)


def _list_pane_ids(workspace_name: str) -> list[str] | None:
    """Return current pane IDs (positional order) or None on soft failure.

    None is returned when tmux is unavailable OR list-panes hits a hard
    error; the helpers above use None as the "abandon this hook" signal.

    NB (kaizen#61): ``workspace_name`` is intentionally NOT passed to tmux
    as a target — it is a CC-internal team identifier, not a tmux
    session/window name. CC's team-mode panes are created inside the
    orchestrator's current tmux window (the same window the
    ``kaizen:improve`` invocation runs in), so the default ``current``
    target is what we want. The parameter is retained for log lines and
    for future use if CC ever exposes a real session name.
    """
    proc = _run_tmux(["list-panes", "-F", "#{pane_id}"])
    if _tmux_unavailable(proc):
        return None
    if proc.returncode != 0:
        print(
            f"[_tmux_workspace] list-panes for {workspace_name!r} failed: "
            f"{(proc.stderr or '').strip()}",
            file=sys.stderr,
        )
        return None
    pane_ids = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    # kaizen#66 — exclude the orchestrator's own pane at the source so the
    # positional zip against ``ordered_agents`` does not re-classify the PM
    # as a teammate. ``TMUX_PANE`` is set by tmux for every process running
    # inside a pane and is stable for the life of the pane, making it the
    # authoritative "this is my own pane" signal. When unset (CI / headless
    # / non-tmux runs) we return the full list and the caller falls back to
    # the older reactive swap-pane path.
    lead_pane_id = _orchestrator_pane_id()
    if lead_pane_id and lead_pane_id in pane_ids:
        pane_ids = [pid for pid in pane_ids if pid != lead_pane_id]
    return pane_ids


def apply_workspace_layout(
    workspace_name: str,
    *,
    ordered_agents: list[str],
    main_agent: str | None = None,
) -> dict[str, str]:
    """Apply "left main + right 2-column grid" layout for ``workspace_name``.

    ``ordered_agents`` is the team's member list in the order passed to
    ``TeamCreate``; we map ``pane_ids[i] → ordered_agents[i]`` to recover
    the per-pane identity that CC's ``general-purpose`` titles obscure.

    Returns the pane_id → agent map. The caller passes this dict to
    ``set_pane_titles`` (and re-uses it across waves to retitle without
    re-applying layout).

    No-op (returns ``{}``) when:
      - tmux server isn't running
      - the workspace doesn't exist
      - ``ordered_agents`` is empty

    Best-effort: failures during swap / join surface a single stderr
    warning each and we proceed — a partial layout is better than no
    layout, and a kaizen cycle must not abandon because of a tmux quirk.
    """
    if not ordered_agents:
        return {}

    pane_ids = _list_pane_ids(workspace_name)
    if pane_ids is None or not pane_ids:
        return {}

    # Positional zip — extra panes (e.g. an orchestrator pane CC may
    # interleave in the future) get no agent mapping and are left alone.
    pane_to_agent: dict[str, str] = {
        pane_ids[i]: ordered_agents[i] for i in range(min(len(pane_ids), len(ordered_agents)))
    }

    # kaizen#66 — pin the orchestrator's pane title to a reserved literal
    # once at workspace boot. ``_list_pane_ids`` already drops the
    # orchestrator pane_id from the returned list (so it does not appear in
    # ``pane_to_agent``); this call targets the pane by its global pane_id
    # so it works regardless of which window is current.
    lead_pane_id = _orchestrator_pane_id()
    if lead_pane_id:
        pin_orchestrator_title(lead_pane_id)

    # Step 1 — main-vertical (1 wide left + N stacked right). Same kaizen#61
    # reasoning as ``_list_pane_ids``: target the orchestrator's current
    # window via tmux's default, not the CC-internal team_name.
    layout_proc = _run_tmux(["select-layout", "main-vertical"])
    if _tmux_unavailable(layout_proc):
        return pane_to_agent
    if layout_proc.returncode != 0:
        print(
            f"[_tmux_workspace] select-layout main-vertical for {workspace_name!r} "
            f"failed: {(layout_proc.stderr or '').strip()}",
            file=sys.stderr,
        )
        return pane_to_agent

    # Step 2 — reactive swap-pane fallback for the non-tmux / no-TMUX_PANE
    # path. When ``TMUX_PANE`` IS set (the typical interactive case)
    # ``_list_pane_ids`` has already excluded the orchestrator at source so
    # the zip is correct by construction and no swap is needed. The swap
    # path is retained for CI / headless / non-tmux subagent runs where
    # ``TMUX_PANE`` is unset and the orchestrator (if any) may still be
    # interleaved with teammate panes in the list.
    if lead_pane_id is None and main_agent:
        target_pane_id: str | None = next(
            (pid for pid, name in pane_to_agent.items() if name == main_agent),
            None,
        )
        if target_pane_id is not None and pane_ids[0] != target_pane_id:
            swap_proc = _run_tmux(["swap-pane", "-t", pane_ids[0], "-s", target_pane_id])
            if swap_proc.returncode != 0 and not _tmux_unavailable(swap_proc):
                print(
                    f"[_tmux_workspace] swap-pane main<-{main_agent} failed: "
                    f"{(swap_proc.stderr or '').strip()}",
                    file=sys.stderr,
                )
            else:
                # Re-list panes so we know the current left-to-right /
                # top-to-bottom order BEFORE we start joining pairs in
                # the right column.
                relisted = _list_pane_ids(workspace_name)
                if relisted is not None and relisted:
                    pane_ids = relisted

    # Step 3 — fold the right column into a 2-column grid.
    fold_right_column(pane_ids)

    return pane_to_agent


def fold_right_column(pane_ids: list[str]) -> None:
    """Fold a top-to-bottom right column into a 2-column grid via join-pane.

    ``pane_ids[0]`` is the main pane (left); ``pane_ids[1:]`` is the right
    column from top to bottom. Pair (1+2), (3+4), (5+6), ... by joining
    the second of each pair into the first via a horizontal split.

    Designed to be called ONCE per cycle — the caller holds the
    "did I fold yet" flag (e.g. ``layout_applied`` in
    ``scripts.team_executor.team_cycle_executor``). Re-folding would
    undo earlier joins, so this helper does not self-gate.

    Tolerant of "no tmux server" / individual join failures: surfaces a
    single stderr warning per failed pair and proceeds.
    """
    right_panes = pane_ids[1:]
    for i in range(0, len(right_panes) - 1, 2):
        target = right_panes[i]
        source = right_panes[i + 1]
        join_proc = _run_tmux(["join-pane", "-h", "-s", source, "-t", target])
        if join_proc.returncode != 0 and not _tmux_unavailable(join_proc):
            print(
                f"[_tmux_workspace] join-pane -h -s {source} -t {target} failed: "
                f"{(join_proc.stderr or '').strip()}",
                file=sys.stderr,
            )


def pin_orchestrator_title(pane_id: str, glyph: str | None = None) -> None:
    """Pin the orchestrator's pane title to the reserved PM literal.

    Idempotent: calling twice with the same effective glyph is a no-op
    from the user's perspective. ``glyph`` defaults to ``KAIZEN_PM_PANE_GLYPH``
    (env), then ``"●"`` (U+25CF) — callers normally don't pass it.

    The orchestrator pane is identified by ``$TMUX_PANE`` at the caller
    (see :func:`_orchestrator_pane_id`); this helper just sets the title.
    Tolerant of "tmux server not running" / "pane gone" — never raises.
    """
    if glyph is None:
        title = _pm_pane_title()
    else:
        title = f"{glyph}{_PM_PANE_LABEL_SUFFIX}" if glyph else _PM_PANE_LABEL_SUFFIX.lstrip()
    set_pane_title(pane_id, title)


def set_pane_title(pane_id: str, title: str) -> None:
    """Set the title of one pane by global pane_id. Sanitized + idempotent.

    Uses ``select-pane -t %N -T <sanitized>`` which targets the pane
    globally (pane_ids are unique across sessions on a tmux server), so
    this works regardless of which window/session is "current."

    The title is run through :func:`_sanitize_title` before being passed
    to tmux — strips control / bidi chars, escapes ``#`` to ``##`` (so
    tmux does not interpret format specifiers), and left-truncates to
    the 64-char tmux soft limit. A ``[wN] `` wave prefix is preserved.

    Tolerant of "tmux server not running" / "pane gone": no exception is
    raised; a single stderr warning is emitted on hard failures.
    """
    sanitized = _sanitize_title(title)
    proc = _run_tmux(["select-pane", "-t", pane_id, "-T", sanitized])
    if proc.returncode == 0:
        return
    if _tmux_unavailable(proc):
        return
    print(
        f"[_tmux_workspace] select-pane -T {shlex.quote(sanitized)} on {pane_id} failed: "
        f"{(proc.stderr or '').strip()}",
        file=sys.stderr,
    )


def set_pane_titles(workspace_name: str, pane_to_title: dict[str, str]) -> None:
    """Set ``pane_title`` for each ``{pane_id: title}`` in ``pane_to_title``.

    Targets panes by ``pane_id`` (the ``%N`` identifier returned by
    ``list-panes``), so this is robust to layout changes — once we have
    the map from :func:`apply_workspace_layout` we can retitle across
    waves without re-listing panes.

    Tolerant of:
      - tmux server not running (single early return on the first call)
      - individual panes having disappeared (single stderr warning, keep
        going for the rest of the dict)

    ``workspace_name`` is unused at runtime — ``select-pane -t %N`` works
    on the global pane_id — but accepted for symmetry with
    ``apply_workspace_layout`` and for future flexibility (e.g. if we
    ever switch to ``-t {workspace}.{index}`` style targeting).
    """
    del workspace_name  # currently unused; see docstring
    for pane_id, title in pane_to_title.items():
        sanitized = _sanitize_title(title)
        proc = _run_tmux(["select-pane", "-t", pane_id, "-T", sanitized])
        if proc.returncode == 0:
            continue
        if _tmux_unavailable(proc):
            # Short-circuit: server is gone — no point continuing through
            # the rest of the dict.
            return
        print(
            f"[_tmux_workspace] select-pane -T {shlex.quote(sanitized)} on {pane_id} failed: "
            f"{(proc.stderr or '').strip()}",
            file=sys.stderr,
        )
