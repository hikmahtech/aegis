from aegis_comms.adapters.base import CardSpec
from aegis_comms.adapters.slack import _note_from_state
from aegis_comms.cards import render_slack_blocks


def _c(kind, options=None, prompt="p"):
    return CardSpec(
        interaction_id="i1", agent_id="sebas", kind=kind, prompt=prompt, options=options
    )


def _input_blocks(blocks):
    return [b for b in blocks if b["type"] == "input"]


def test_button_kinds_get_optional_note_input_before_actions():
    for kind, options in (
        ("approval", None),
        ("choice", {"k1": "Go"}),
        ("ack", None),
    ):
        blocks = render_slack_blocks(_c(kind, options))
        inputs = _input_blocks(blocks)
        assert len(inputs) == 1, kind
        note = inputs[0]
        assert note["block_id"] == "correction_note"
        assert note["optional"] is True
        assert note["element"]["action_id"] == "note"
        # input renders above the actions row so it's visible before tapping
        assert blocks.index(note) < blocks.index(
            next(b for b in blocks if b["type"] == "actions")
        )


def test_url_only_kinds_have_no_note_input():
    for kind, options in (
        ("input", {"aegis_ui_url": "http://x"}),
        ("draft_review", {"aegis_ui_url": "http://x"}),
    ):
        blocks = render_slack_blocks(_c(kind, options))
        assert _input_blocks(blocks) == [], kind


def test_note_from_state_extracts_trimmed_text():
    body = {
        "state": {
            "values": {
                "correction_note": {"note": {"value": "  wrong label — work task  "}}
            }
        }
    }
    assert _note_from_state(body) == "wrong label — work task"


def test_note_from_state_tolerates_missing_state():
    assert _note_from_state({}) == ""
    assert _note_from_state({"state": {"values": {}}}) == ""
    assert _note_from_state({"state": {"values": {"correction_note": {}}}}) == ""
