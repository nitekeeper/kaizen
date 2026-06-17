"""Headless ``KAIZEN_TRANSPORT=host`` cycle entrypoint (M8 glue PR — Option A).

This is the GLUE between the subagent cycle (``internal/cycle/SKILL.md`` prose)
and atelier's in-process host engine. It is NOT a re-implementation of the Phase
1-3 meeting — the meeting stays in-prose, produces the Action-Items DAG, and the
SKILL hands that DAG here.

Flow (Option A — wire host at the SKILL layer, NOT run.py):

  1. The SKILL's Phase 1-3 meeting produces the kaizen-NATIVE Action-Items DAG
     (one item per change, keys ``id, touches, reads, depends_on, wave`` + optional
     ``owner``). The agent writes it as JSON to a file in the clone (typically
     ``.ai/host_action_items.json`` — gitignored) and invokes::

         PYTHONPATH=. python3 -m scripts.host_cycle_entry \
             --action-items-file <path> \
             --clone-dir <clone> \
             --subject "<cycle subject>" \
             --roster backend-engineer-1 sdet-1 ... \
             --pm pm-1 \
             --cycle-n 1 [--run-id 7] \
             --test-command pytest

  2. This entry:
       a. resolves + GUARDS the transport (``require_wired_transport(allow_host=True)``
          — host is wired ONLY for THIS entrypoint, RISK-4); rejects when
          ``KAIZEN_TRANSPORT`` != ``host``.
       b. reads the DAG JSON and FAILS FAST if it carries engine-shaped keys
          (``task_id`` / ``parallel_group`` / ``writes`` / ``assigned_persona`` /
          ``phase``) — those are the OUTPUT of ``build_engine_tasks``. Feeding them
          to ``validate_dag`` trips ``_check_item_shape``, which RAISES a bare
          ``ValueError`` deep inside ``host_cycle_executor`` (it calls validate_dag
          with no try/except) — an opaque, key-by-key message far from the cause. The
          guard surfaces a clear, actionable error that NAMES the offending engine
          key(s) BEFORE that deeper error can fire. (A genuine ``no_consensus`` is a
          different thing entirely: it fires only for the semantic DAG gates on a
          WELL-shaped DAG, never for a shape error.)
       c. computes ``existing_files`` from the clone (REUSE
          ``scripts.team_executor._collect_existing_files`` — the same source the
          team path feeds to ``validate_dag`` gate 3).
       d. calls :func:`scripts.host_executor.host_cycle_executor` (which runs Phase
          4 + the Phase 5b' review→fix loop + the CI-mirror gate AND commits
          INTERNALLY before returning — F3 holds with NO second commit here).
       e. emits the outcome dict as JSON on stdout for the SKILL agent to read back
          as the cycle outcome.

The outcome dict shape is exactly ``host_cycle_executor``'s (== team mode's):
success carries ``status/subject/commit_sha/minutes_memex_slug/participants``;
abandoned carries ``status/subject/participants/phase_reached/reason/detail/
artifacts`` (+ the four review-outcome keys when the abandon came from the review
loop — ``review_iteration_count/unresolved_findings/convergence_summary/
reviewer_attribution``, the channel through which ``peer_unconfirmed`` is surfaced).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from scripts.transport import require_wired_transport

# Engine-task keys that are the OUTPUT of ``scripts.host_executor.build_engine_tasks``,
# NOT valid kaizen-native Action-Item input keys. If a DAG handed to us carries any
# of these, the agent serialized the wrong shape (engine tasks instead of the native
# DAG) — fail fast with an actionable, key-naming message rather than letting it trip
# ``validate_dag``'s deeper, opaque ``ValueError`` (raised uncaught inside
# ``host_cycle_executor``).
_ENGINE_SHAPED_KEYS: frozenset[str] = frozenset(
    {"task_id", "parallel_group", "writes", "assigned_persona", "phase"}
)

# The kaizen-native required keys (mirrors scripts.dag._REQUIRED_KEYS). Declared here
# only for the worked-example / error text — dag.validate_dag remains the single
# source of truth for actual shape validation.
_NATIVE_REQUIRED_KEYS: tuple[str, ...] = ("id", "touches", "reads", "depends_on", "wave")


class ActionItemsShapeError(ValueError):
    """The handed-in Action-Items DAG is not the kaizen-native shape.

    Raised on two surfaces, BOTH the operator-input surface that should map to a
    clean exit 2:
      * ``_assert_native_shape`` — engine-shaped keys (the most common
        mis-serialization) or a non-list / non-dict payload, with a clear key-naming
        message;
      * ``run_host_cycle``'s controlled pre-validate (step c2) — a native-LOOKING but
        malformed DAG (missing required key / wrong type / duplicate id), where the
        bare ``ValueError`` from ``validate_dag`` is REMAPPED to this type.

    Both surface a clear, actionable error rather than ``validate_dag``'s deeper,
    opaque ``ValueError``. ``main()`` catches THIS type (not a bare ``ValueError``),
    so kaizen's own fail-loud ``ValueError`` guards inside ``host_cycle_executor``
    (``model_for`` on an unknown phase, ``_severity_rank``, ``Finding``) still crash
    loudly. (A ``no_consensus`` abandon is unrelated: it is a RETURNED dict from the
    semantic gates on a well-shaped DAG, never a raised error.)
    """


def _assert_native_shape(items: object) -> list[dict]:
    """Fail fast unless ``items`` is a list of kaizen-native Action-Item dicts.

    Catches the engine-shaped-keys mis-serialization the architect flagged
    (RISK-1) with a clear, key-naming error. Does NOT duplicate ``validate_dag``'s
    gates — it only rejects the coarse "wrong shape entirely" cases (non-list,
    non-dict item, engine-shaped keys). A native-LOOKING but malformed DAG (missing
    a required key, wrong-typed ``touches``) flows past here and is caught instead by
    ``run_host_cycle``'s controlled pre-validate (step c2), which maps
    ``validate_dag``'s ``ValueError`` to :class:`ActionItemsShapeError` for the same
    clean exit-2. ``validate_dag`` (inside ``host_cycle_executor``) still runs the
    full 4-gate + per-item-shape validation.
    """
    if not isinstance(items, list):
        raise ActionItemsShapeError(
            f"action-items payload must be a JSON list of items, got "
            f"{type(items).__name__}. Expected a list like the worked example in "
            f"internal/cycle/SKILL.md (kaizen-native keys: "
            f"{', '.join(_NATIVE_REQUIRED_KEYS)})."
        )
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise ActionItemsShapeError(
                f"action item {idx} is {type(item).__name__}, expected a dict with "
                f"kaizen-native keys: {', '.join(_NATIVE_REQUIRED_KEYS)}."
            )
        engine_keys = _ENGINE_SHAPED_KEYS & set(item)
        if engine_keys:
            raise ActionItemsShapeError(
                f"action item {idx} carries engine-shaped key(s) "
                f"{sorted(engine_keys)} — those are the OUTPUT of "
                f"build_engine_tasks, not valid Action-Item INPUT. Emit the "
                f"kaizen-native DAG instead (keys: "
                f"{', '.join(_NATIVE_REQUIRED_KEYS)} + optional 'owner'). See the "
                f"worked example in internal/cycle/SKILL.md."
            )
    return items


def run_host_cycle(
    *,
    action_items_file: str | Path,
    clone_dir: str | Path,
    subject: str | None,
    roster: list[str] | None,
    pm: str | None,
    cycle_n: int,
    run_id: int | None,
    test_command: str,
    env=None,
    runner=None,
) -> dict:
    """Resolve+guard the transport, load the DAG, and run the host cycle.

    Returns ``host_cycle_executor``'s outcome dict (success or abandoned). Raises
    :class:`NotImplementedError` (via the transport guard) when ``KAIZEN_TRANSPORT``
    is not ``host``, and :class:`ActionItemsShapeError` on an engine-shaped /
    malformed DAG. ``runner`` is injectable for tests (a ``FakeCliRunner``); in
    production it defaults to ``None`` → atelier's ``real_cli_runner`` in-window.
    """
    # (a) Transport guard — host is wired ONLY for this entrypoint (RISK-4).
    # Rejects KAIZEN_TRANSPORT=bridge/unset (this script must not run them) and
    # surfaces UnknownTransportError for a typo.
    transport = require_wired_transport(env, allow_host=True)
    if transport != "host":
        raise NotImplementedError(
            f"scripts.host_cycle_entry runs ONLY under KAIZEN_TRANSPORT=host; "
            f"resolved transport={transport!r}. The bridge/default path runs the "
            f"cycle in-prose (see internal/cycle/SKILL.md) — do not invoke this "
            f"entry for it."
        )

    # (b) Load + fail-fast shape check on the kaizen-native DAG.
    raw = Path(action_items_file).read_text(encoding="utf-8")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ActionItemsShapeError(
            f"action-items file {action_items_file!r} is not valid JSON: {exc}"
        ) from exc
    action_items = _assert_native_shape(parsed)

    # (c) Compute existing_files from the clone — REUSE the team path's source so
    # validate_dag gate 3 (reads satisfiable) sees the SAME file set.
    from scripts.team_executor import _collect_existing_files

    existing_files = _collect_existing_files(Path(clone_dir))

    # (c2) Pre-validate the DAG SHAPE at a controlled point, mapping the
    # input-shape `ValueError` surface to `ActionItemsShapeError`. `validate_dag`
    # RAISES `ValueError` for malformed INPUT (missing required key / wrong type via
    # `_check_item_shape`; duplicate id via `topological_waves`) and RETURNS
    # `ok=False` for the semantic gates (cycle / contention / unsatisfiable reads /
    # orphan deps) — it never raises for those. So catching `ValueError` HERE
    # captures exactly the operator-input surface and nothing else.
    #
    # Why a controlled pre-validate instead of a broad `except ValueError` around
    # the executor call: `host_cycle_executor` raises its OWN deliberate fail-loud
    # `ValueError`s (e.g. `model_for` on an unknown phase, `_severity_rank`,
    # `Finding.__post_init__`) — those are kaizen WIRING bugs that MUST surface as a
    # loud crash, not be re-framed to the operator as "fix your input". Validating
    # here (before the executor) keeps `main()`'s catch narrow so those propagate.
    # The executor re-validates internally; the double validate is harmless
    # (`validate_dag` is pure).
    from scripts.dag import validate_dag

    try:
        validate_dag(list(action_items), existing_files=frozenset(existing_files))
    except ValueError as exc:
        raise ActionItemsShapeError(f"Action-Items DAG shape error: {exc}") from exc

    # (d) Hand off to the host executor (commits internally; F3 — no commit here).
    from scripts.host_executor import host_cycle_executor

    return host_cycle_executor(
        action_items=action_items,
        existing_files=existing_files,
        clone_dir=clone_dir,
        roster=roster,
        pm=pm,
        subject=subject,
        cycle_n=cycle_n,
        run_id=run_id,
        test_command=test_command,
        runner=runner,
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="host_cycle_entry",
        description="KAIZEN_TRANSPORT=host cycle entry (M8 glue). Reads a "
        "kaizen-native Action-Items DAG and runs the host engine cycle.",
    )
    parser.add_argument(
        "--action-items-file",
        required=True,
        help="Path to the kaizen-native Action-Items DAG as a JSON list.",
    )
    parser.add_argument("--clone-dir", required=True, help="The experiment clone path.")
    parser.add_argument("--subject", default=None, help="Cycle subject (or omit).")
    parser.add_argument(
        "--roster",
        nargs="*",
        default=None,
        help="Resolved participant role ids (space-separated). First is PM if --pm omitted.",
    )
    parser.add_argument("--pm", default=None, help="PM role id (defaults to roster[0]).")
    parser.add_argument("--cycle-n", type=int, default=1, help="1-indexed cycle number.")
    parser.add_argument(
        "--run-id",
        type=int,
        default=None,
        help="Kaizen run id (feeds the Memex slug; omit for a host-<n> slug).",
    )
    parser.add_argument(
        "--test-command",
        default="pytest",
        help="The target's CI test command, mirrored by the post-Phase-4 gate (F2).",
    )
    ns = parser.parse_args(argv)

    try:
        outcome = run_host_cycle(
            action_items_file=ns.action_items_file,
            clone_dir=ns.clone_dir,
            subject=ns.subject,
            roster=ns.roster,
            pm=ns.pm,
            cycle_n=ns.cycle_n,
            run_id=ns.run_id,
            test_command=ns.test_command,
        )
    except (ActionItemsShapeError, NotImplementedError) as exc:
        # Actionable, caller-facing errors → stderr + non-zero exit. These are
        # operator/serialization bugs the agent must fix, NOT cycle abandonments.
        #
        # The catch is NARROW BY DESIGN. A native-LOOKING but malformed DAG (missing
        # key / wrong-typed `touches` / duplicate id) is caught EARLY in
        # `run_host_cycle`'s controlled pre-validate (step c2), which maps that
        # input-shape `ValueError` to `ActionItemsShapeError` — so it lands here. We
        # do NOT catch a bare `ValueError`: `host_cycle_executor` raises its OWN
        # deliberate fail-loud `ValueError`s (`model_for` on an unknown phase,
        # `_severity_rank`, `Finding.__post_init__`) for kaizen WIRING bugs, and
        # those MUST crash loudly — not be re-framed to the operator as "fix your
        # input and re-invoke". A genuine cycle abandon is a RETURNED dict
        # (`{"status": "abandoned", ...}`), never a raised exception, so it flows
        # through to the JSON emission below untouched. `UnknownTransportError` is a
        # RuntimeError (not a ValueError) and is intentionally left to propagate.
        print(f"host_cycle_entry: {exc}", file=sys.stderr)
        return 2

    # stdout carries ONLY the outcome dict as JSON — the SKILL agent reads it back
    # as the cycle outcome. ``default=str`` keeps any stray Path/non-JSON value
    # serialisable (the host outcome is plain dict/str/int, but be defensive).
    print(json.dumps(outcome, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
