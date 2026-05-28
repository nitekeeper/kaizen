<!--
_soft_drop_absent.md — shared soft-drop / absent-teammate clause (#83).

Included by the Phase-3 synthesis templates (phase_3_synthesis_star.md,
phase_3_close_star.md). When the per-phase quorum in
scripts/cc_tool_bridge.py soft-drops a silent teammate, that teammate's
entry carries a response that BEGINS with the literal sentinel
`<SOFT-DROPPED:` (scripts/bridge_softdrop.SOFT_DROP_SENTINEL). This clause
tells the synthesising agent to treat such a slot as ABSENT — silence, not
assent — and never to fabricate the missing teammate's position.

The literal `<SOFT-DROPPED:` below MUST stay byte-identical to
SOFT_DROP_SENTINEL; tests/test_dispatch_templates_softdrop.py pins the
template<->constant contract so it cannot silently drift.
-->

Soft-drop note: any entry above whose text begins with the literal `<SOFT-DROPPED:` is a teammate who did NOT reply within this cycle's budget. Treat that seat as ABSENT — it is silence, never assent. Do NOT infer, reconstruct, or fabricate the missing teammate's position; synthesise only from the entries that actually arrived, and note the absence explicitly.
