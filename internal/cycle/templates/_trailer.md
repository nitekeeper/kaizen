<!--
_trailer.md — shared reply contract + shutdown handshake.

This partial is the single source of truth for the F7 reply contract
(GAP-2, run-21) and the GAP-7 shutdown handshake. Every Phase 1-7
teammate-bound dispatch template includes this file by reference via:

    {{ include: _trailer.md }}

Wiring of the include directive into the actual prompt-render path is
performed by `scripts/dispatch_templates.py` via the `_render()`
pipeline's `_resolve_includes` step — this .md partial IS the live byte
source for every teammate-bound dispatch body. Splitting the prose into
a Python constant (`TEAMMATE_REPLY_RULE`) is deferred to this follow-up
issue (kaizen#62); until that split lands, the constants `_REPLY_RULE`
/ `_SHUTDOWN_RULE` / `TEAMMATE_REPLY_RULE` in
`scripts/dispatch_templates.py` must be kept byte-identical to the
prose below — the parity test in
`tests/test_trailer_md_parity.py` is the enforcing contract.

Cog-sci Concern 1: keep the trailer in ONE file so an edit to the
reply contract or the shutdown handshake updates every phase at once;
copy-paste duplication is the slip-class regression vector this
partial exists to eliminate.
-->

IMPORTANT — Reply contract: When you complete your task, you MUST send your response back via SendMessage(to="team-lead", message=<your reply>). The `to` value is literally the string "team-lead" — every team has a registered team-lead agent (the implicit lead_agent_id emitted by TeamCreate). Do NOT just go idle — in CC team mode, spawn-prompt output is not auto-relayed, so silent completion means team-lead never sees your output. Even a brief 'No issues to report' SendMessage is required to advance the cycle. Abandon signals also go via SendMessage — start the body with 'ABANDON: <one-line reason>'. Do not skip the SendMessage even when abandoning. ALSO: if you receive a JSON message body whose first non-whitespace characters are `{"type":"shutdown_request"`, this is a PROTOCOL message (NOT a conversational one). Parse it as JSON, extract its `request_id` field, and respond via SendMessage with a JSON STRING literal body: SendMessage(to="team-lead", message='{"type":"shutdown_response","request_id":"<paste-the-exact-uuid-here>","approve":true}'). Set the `request_id` value to the EXACT uuid string from the incoming request's `request_id` field — copy it verbatim, do NOT alter, truncate, or wrap it in any other structure. The `message=` value MUST be a STRING (single-quoted JSON literal as shown), NOT a dict and NOT a JSON() function call (no such function exists in the tool-call syntax). Set `approve` to true by default; only set `approve` to false (with a one-line reason appended to the JSON) if you are mid-task — where mid-task is DEFINED as: you currently have an in-flight tool call OTHER than this SendMessage. Having already replied to your phase prompt does NOT count as mid-task; approve=true is the default. Approving terminates your process per CC tool contract. Do NOT respond to a shutdown_request with prose.
