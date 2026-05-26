"""Tests for ``scripts.team_executor._apply_pane_label`` — the per-pane
retitle decision predicate that R1-2 (Phase 5b' major) replaced the
prior one-shot ``titled_recipients: set[str]`` pattern with.

The bug being guarded against: under the prior set-of-roles pattern,
ANY recipient whose role had ever been added to ``titled_recipients``
silently skipped every subsequent retitle, even if the desired label
had changed. This made Phase 5b' reviewer panes keep their Phase 4
``[w{n}]`` label (for reviewer-was-also-implementer) or bare role-name
label (for reviewer-was-not-implementer) regardless of how many fix-
loop iterations ran — the operator could not visually distinguish a
fix-loop iteration from a past wave.

The fix: track CURRENT pane labels in a ``dict[str, str]`` and retitle
iff ``desired_title != current_title[recipient]``. The regression
guard below confirms that calling the retitle helper twice with the
SAME recipient but DIFFERENT desired labels fires ``set_pane_title``
on the second call — the exact case the prior set-based gate broke.
"""

from __future__ import annotations

from scripts import _tmux_workspace, team_executor


def test_apply_pane_label_fires_set_pane_title_on_first_call(monkeypatch):
    """Initial label dictates an actual tmux retitle (no prior label)."""
    calls: list[tuple[str, str]] = []

    def fake_set(pane_id: str, title: str) -> None:
        calls.append((pane_id, title))

    monkeypatch.setattr(team_executor, "set_pane_title", fake_set)

    current_title: dict[str, str] = {}
    pane_to_agent = {"%1": "backend-engineer-1"}

    issued = team_executor._apply_pane_label(
        "backend-engineer-1",
        "[w1] backend-engineer-1",
        current_title,
        pane_to_agent,
    )
    assert issued is True
    assert calls == [("%1", "[w1] backend-engineer-1")]
    assert current_title == {"backend-engineer-1": "[w1] backend-engineer-1"}


def test_apply_pane_label_noops_when_label_unchanged(monkeypatch):
    """Idempotency: repeating the SAME desired label is a no-op."""
    calls: list[tuple[str, str]] = []

    def fake_set(pane_id: str, title: str) -> None:
        calls.append((pane_id, title))

    monkeypatch.setattr(team_executor, "set_pane_title", fake_set)

    current_title: dict[str, str] = {"backend-engineer-1": "[w1] backend-engineer-1"}
    pane_to_agent = {"%1": "backend-engineer-1"}

    issued = team_executor._apply_pane_label(
        "backend-engineer-1",
        "[w1] backend-engineer-1",
        current_title,
        pane_to_agent,
    )
    assert issued is False
    assert calls == []
    # Dict unchanged.
    assert current_title == {"backend-engineer-1": "[w1] backend-engineer-1"}


def test_apply_pane_label_fires_again_when_label_changes(monkeypatch):
    """R1-2 regression guard: second call with SAME recipient + DIFFERENT
    desired label MUST issue a tmux retitle.

    This is the exact case the prior ``titled_recipients: set[str]``
    pattern broke. Under that pattern the second call would early-return
    because the recipient was already in the set. Under the dict pattern
    the predicate compares against the CURRENT label, so a different
    desired label fires the retitle.
    """
    calls: list[tuple[str, str]] = []

    def fake_set(pane_id: str, title: str) -> None:
        calls.append((pane_id, title))

    monkeypatch.setattr(team_executor, "set_pane_title", fake_set)

    # Start with the recipient already labeled as Phase 4 wave 1.
    current_title: dict[str, str] = {"sdet-1": "[w1] sdet-1"}
    pane_to_agent = {"%1": "sdet-1"}

    # Phase 5b' iteration 2 brings a different label.
    issued = team_executor._apply_pane_label(
        "sdet-1",
        "[R2] sdet-1",
        current_title,
        pane_to_agent,
    )
    assert issued is True, (
        "R1-2 regression: changing the desired label MUST trigger a "
        "retitle call; under the prior one-shot set pattern it would "
        "have silently no-op'd."
    )
    assert calls == [("%1", "[R2] sdet-1")]
    assert current_title == {"sdet-1": "[R2] sdet-1"}


def test_apply_pane_label_three_label_progression_w1_to_w2_to_R1(monkeypatch):
    """Full cycle progression: bare role → ``[w1]`` → ``[w2]`` → ``[R1]``.

    Every transition fires a real tmux retitle; the same-label repeats
    in between would be no-ops (verified separately above).
    """
    calls: list[tuple[str, str]] = []

    def fake_set(pane_id: str, title: str) -> None:
        calls.append((pane_id, title))

    monkeypatch.setattr(team_executor, "set_pane_title", fake_set)

    current_title: dict[str, str] = {"be-1": "be-1"}  # initial bare-role label
    pane_to_agent = {"%1": "be-1"}

    for desired in ("[w1] be-1", "[w2] be-1", "[R1] be-1"):
        team_executor._apply_pane_label("be-1", desired, current_title, pane_to_agent)

    # Every transition issued a retitle; the predicate didn't swallow any.
    assert calls == [
        ("%1", "[w1] be-1"),
        ("%1", "[w2] be-1"),
        ("%1", "[R1] be-1"),
    ]
    assert current_title == {"be-1": "[R1] be-1"}


def test_apply_pane_label_returns_false_when_pane_to_agent_empty(monkeypatch):
    """Tmux layout never applied (empty pane_to_agent) → no-op, no raise."""
    calls: list[tuple[str, str]] = []

    def fake_set(pane_id: str, title: str) -> None:
        calls.append((pane_id, title))

    monkeypatch.setattr(team_executor, "set_pane_title", fake_set)

    current_title: dict[str, str] = {}
    pane_to_agent: dict[str, str] = {}

    issued = team_executor._apply_pane_label("anyone", "[w1] anyone", current_title, pane_to_agent)
    assert issued is False
    assert calls == []
    # current_title NOT updated when no tmux call was made.
    assert current_title == {}


def test_apply_pane_label_returns_false_when_recipient_not_in_map(monkeypatch):
    """Positional zip mismatched the roster (CC reordered panes) →
    no-op for the unmapped recipient.
    """
    calls: list[tuple[str, str]] = []

    def fake_set(pane_id: str, title: str) -> None:
        calls.append((pane_id, title))

    monkeypatch.setattr(team_executor, "set_pane_title", fake_set)

    current_title: dict[str, str] = {}
    pane_to_agent = {"%1": "backend-engineer-1"}  # only be-1 mapped

    issued = team_executor._apply_pane_label(
        "security-engineer-1",  # not in pane_to_agent
        "[R1] security-engineer-1",
        current_title,
        pane_to_agent,
    )
    assert issued is False
    assert calls == []
    assert current_title == {}


def test_apply_pane_label_does_not_swallow_per_recipient_state(monkeypatch):
    """Two recipients track independent labels — relabeling one MUST NOT
    incidentally update the other's bookkeeping.
    """
    calls: list[tuple[str, str]] = []

    def fake_set(pane_id: str, title: str) -> None:
        calls.append((pane_id, title))

    monkeypatch.setattr(team_executor, "set_pane_title", fake_set)

    current_title: dict[str, str] = {"a": "[w1] a", "b": "[w1] b"}
    pane_to_agent = {"%1": "a", "%2": "b"}

    # Relabel only `a`.
    team_executor._apply_pane_label("a", "[R1] a", current_title, pane_to_agent)
    # b's label MUST be unchanged.
    assert current_title == {"a": "[R1] a", "b": "[w1] b"}
    # And calling for b with its EXISTING label still no-ops.
    issued = team_executor._apply_pane_label("b", "[w1] b", current_title, pane_to_agent)
    assert issued is False


def test_apply_pane_label_uses_pane_id_targeting_via_set_pane_title(monkeypatch):
    """The helper MUST pass the recipient's pane_id (``%N``) to
    ``set_pane_title`` — global pane-id targeting is what survives the
    kaizen#61 ``-t workspace_name`` drop.
    """
    seen: list[str] = []

    def fake_set(pane_id: str, title: str) -> None:
        seen.append(pane_id)

    monkeypatch.setattr(team_executor, "set_pane_title", fake_set)

    current_title: dict[str, str] = {}
    pane_to_agent = {"%7": "arch-1"}

    team_executor._apply_pane_label("arch-1", "x", current_title, pane_to_agent)
    assert seen == ["%7"], "must target by pane_id, not by role name"


def test_set_pane_title_module_reference_is_live(monkeypatch):
    """Sanity: ``team_executor.set_pane_title`` is the same callable that
    ``scripts._tmux_workspace.set_pane_title`` exposes (i.e. the import
    is a real reference, not a stale alias). This protects against a
    future refactor that accidentally rebinds one name without the
    other.
    """
    assert team_executor.set_pane_title is _tmux_workspace.set_pane_title
