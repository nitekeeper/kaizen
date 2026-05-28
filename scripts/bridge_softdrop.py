"""Soft-drop record shape for the per-phase quorum in the queue bridge.

Single source of truth for what a *soft-dropped* teammate response looks
like. A soft-drop is the synthetic row the bridge substitutes for a teammate
that never reached ``status='ready'`` before the per-row soft-timeout, once
the batch has met its quorum (see ``cc_tool_bridge.QueueBridgeWrapper``). It
forgives *silence* — it is NOT used for ``status='error'`` / disappeared rows,
which keep hard-error semantics.

Layering: this is a low-level module. It MUST NOT import from
``dispatch_templates`` or any higher-level orchestration module — the bridge
sits below the prompt/template layer, and the synthesis template *references*
this constant, never the reverse.

The record carries the soft-drop signal in TWO redundant forms, by design:

* ``soft_dropped: True`` — the *authoritative*, machine-checkable flag.
  Quorum and synthesis code MUST branch on this boolean, not on the prose.
* ``response`` — a human-readable string that opens with the literal
  ``SOFT_DROP_SENTINEL`` prefix. This is the backup signal that survives any
  path which flattens the record down to its bare ``response`` string (e.g.
  ``send_message_many``), so a synthesis prompt that only sees the string can
  still recognise an absent teammate. It is NOT the primary signal.

Keeping both is deliberate: the boolean is truth for code, the sentinel is a
backstop for prose-only consumers. ``is_soft_drop_record`` checks the boolean.
"""

from __future__ import annotations

from typing import Any

# Literal prefix that marks a soft-drop ``response`` string. A legitimate
# teammate reply must never begin with this token. Referenced verbatim by the
# synthesis dispatch template; pinned by a regression test so the
# bridge<->template contract cannot silently drift. Treat as an API constant —
# do not retype the literal elsewhere, import it.
SOFT_DROP_SENTINEL: str = "<SOFT-DROPPED:"


def make_soft_drop_record(idx: int, recipient: str, reason: str) -> dict[str, Any]:
    """Build the synthetic soft-drop record for one batch slot.

    Parameters
    ----------
    idx:
        Input-order index of the slot within the batch (for attribution).
    recipient:
        The teammate the silent row was addressed to (the ``to`` field of the
        originating message). Preserved on the record so downstream consumers
        keep the input-order/teammate alignment that the batch contract
        guarantees.
    reason:
        Short human-readable explanation of why the row was soft-dropped
        (e.g. ``"row never reached ready before soft-timeout"``).

    Returns
    -------
    dict
        A record shaped like a real bridge response so it can sit in the
        ``responses`` list in input order without breaking alignment:

        ``{"response": "<SOFT-DROPPED: ...>", "soft_dropped": True, "to": recipient}``

        ``response`` is always a ``str`` opening with ``SOFT_DROP_SENTINEL`` so
        the ``send_message_many`` unwrap (which requires a ``response`` string)
        does not raise on a soft-drop, and ``soft_dropped`` is the authoritative
        flag for quorum/synthesis branching.
    """
    response = f"{SOFT_DROP_SENTINEL} {reason} (recipient={recipient!r}, idx={idx})>"
    return {"response": response, "soft_dropped": True, "to": recipient}


def is_soft_drop_record(record: Any) -> bool:
    """Return ``True`` iff ``record`` is a soft-drop record.

    Authoritative check: branches on the ``soft_dropped`` boolean flag, NOT on
    the prose sentinel. The sentinel string is only a backup for consumers that
    have already lost the structured record. Code with access to the dict MUST
    use this helper (or read the flag directly), never regex the response.
    """
    return isinstance(record, dict) and record.get("soft_dropped") is True
