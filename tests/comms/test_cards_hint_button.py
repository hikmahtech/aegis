from aegis_comms.adapters.base import CardSpec
from aegis_comms.cards import render_slack_blocks


def _spec(allow_hint):
    return CardSpec(
        interaction_id="ID1", agent_id="pandoras-actor", kind="choice",
        prompt="Which repo?", options={"0": "aegis", "none": "None"}, allow_hint=allow_hint,
    )


def _buttons(blocks):
    actions = [b for b in blocks if b["type"] == "actions"]
    return actions[0]["elements"] if actions else []


def test_hint_button_present_when_allowed():
    btns = _buttons(render_slack_blocks(_spec(True)))
    hint = [b for b in btns if b.get("action_id") == "hint_open"]
    assert len(hint) == 1
    assert hint[0]["value"] == "interaction:ID1:hint_open"


def test_hint_button_absent_when_not_allowed():
    btns = _buttons(render_slack_blocks(_spec(False)))
    assert not [b for b in btns if b.get("action_id") == "hint_open"]
    # normal option buttons unchanged
    assert any(b.get("action_id") == "interaction_0" for b in btns)
