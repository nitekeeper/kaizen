"""Seam-A vs Seam-B reconciliation under both oracle regimes.

Two INLINE transcript trees are built under ``tmp_path`` (no checked-in fixture
files): each has an orchestrator transcript plus a sidechain (sub-agent)
transcript. Seam B (:mod:`scripts.tokenmeter_transcript`) walks the trees and
INCLUDES the sidechain tokens — it is ALWAYS the authoritative headline. Seam A is
the cost oracle, used for validation only.

We assert reconciliation is correct under BOTH oracle regimes:

* **Seam-A aggregates** — the oracle total covers orchestrator + sub-agent. It
  matches the full (sidechain-included) computed total → ``agree``.
* **orchestrator-only** — the oracle total covers only the orchestrator share. It
  diverges from the full computed total, and the gap is attributed to the
  ``subagent-boundary`` discriminator (not to ``pricing``).
"""

from __future__ import annotations

import json

from scripts.tokenmeter_pricing import cost_usd
from scripts.tokenmeter_schema import reconcile_cost
from scripts.tokenmeter_transcript import collect_usage_records

MODEL = "claude-opus-4-7"

# Orchestrator: modest spend. Sub-agent (sidechain): larger spend, so the
# orchestrator-only regime diverges well past the 5% HARD threshold.
_ORCH = {
    "input_tokens": 1000,
    "output_tokens": 500,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
}
_SUB = {
    "input_tokens": 2000,
    "output_tokens": 1000,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
}


def _assistant_line(session_id, message_id, usage, *, sidechain=False):
    obj = {
        "type": "assistant",
        "sessionId": session_id,
        "message": {"id": message_id, "usage": usage},
    }
    if sidechain:
        obj["isSidechain"] = True
    return json.dumps(obj)


def _build_tree(base):
    """Build one transcript tree: an orchestrator file + a sidechain sub-agent file."""
    project = base / "projects" / "proj"
    subagents = project / "subagents"
    subagents.mkdir(parents=True)

    (project / "session-main.jsonl").write_text(
        _assistant_line("sess-main", "m1", _ORCH) + "\n",
        encoding="utf-8",
    )
    # The sidechain file lives under <project>/subagents/; Seam B recovers the
    # parent ("proj") from the path and counts its tokens against the run.
    (subagents / "agent-x.jsonl").write_text(
        _assistant_line("sub-sess", "m2", _SUB, sidechain=True) + "\n",
        encoding="utf-8",
    )
    return base


def _computed_full_and_orchestrator(records):
    full = sum(cost_usd(r.usage, MODEL).total_cost for r in records)
    orchestrator = sum(cost_usd(r.usage, MODEL).total_cost for r in records if not r.is_sidechain)
    return full, orchestrator


def test_reconcile_seam_a_aggregates_regime(tmp_path):
    base = _build_tree(tmp_path / "tree_aggregate")
    records = collect_usage_records(config_dir=base)

    assert len(records) == 2
    assert any(r.is_sidechain for r in records)  # sidechain INCLUDED in Seam B

    full, _orchestrator = _computed_full_and_orchestrator(records)

    # Seam A aggregates orchestrator + sub-agent -> oracle == full computed total.
    block = reconcile_cost(records, {"total_cost_usd": full}, MODEL)
    assert block["reconciled"] == "agree"
    assert block["computed_total_cost_usd"] > 0
    assert block["seam_a_total_cost_usd"] is not None
    assert block["blocks_validated"] is False


def test_reconcile_orchestrator_only_regime(tmp_path):
    base = _build_tree(tmp_path / "tree_orchestrator_only")
    records = collect_usage_records(config_dir=base)

    full, orchestrator = _computed_full_and_orchestrator(records)
    assert orchestrator < full  # the sub-agent share is the difference

    # Seam A only saw the orchestrator share -> it diverges from the full headline,
    # and the gap is the subagent boundary, NOT a pricing error.
    block = reconcile_cost(records, {"total_cost_usd": orchestrator}, MODEL)
    assert block["reconciled"] in ("soft", "hard")
    assert block["divergence_cause"] == "subagent-boundary"
    # BOTH totals are always recorded.
    assert block["seam_a_total_cost_usd"] is not None
    assert block["computed_total_cost_usd"] is not None
    assert block["computed_total_cost_usd"] > block["seam_a_total_cost_usd"]
