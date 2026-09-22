import json

from aegis_comms.slack_modal import (
    TEXT_ANSWER_MAX,
    build_hint_modal,
    build_text_modal,
    parse_text_submission,
    parse_view_submission,
)


def test_build_hint_modal_shape():
    view = build_hint_modal("ID9", "DB down on news-service")
    assert view["type"] == "modal"
    assert view["callback_id"] == "hint_submit"
    assert view["private_metadata"] == "ID9"
    input_blocks = [b for b in view["blocks"] if b.get("block_id") == "hint"]
    assert input_blocks and input_blocks[0]["element"]["action_id"] == "value"


def test_parse_view_submission_extracts_id_and_text():
    payload = {
        "view": {
            "callback_id": "hint_submit",
            "private_metadata": "ID9",
            "state": {"values": {"hint": {"value": {"value": "acme/news-service"}}}},
        }
    }
    assert parse_view_submission(payload) == ("ID9", "acme/news-service")


def test_parse_view_submission_rejects_other_callbacks():
    assert parse_view_submission({"view": {"callback_id": "other"}}) is None


def test_parse_view_submission_rejects_empty_text():
    payload = {"view": {"callback_id": "hint_submit", "private_metadata": "ID9",
                        "state": {"values": {"hint": {"value": {"value": "   "}}}}}}
    assert parse_view_submission(payload) is None


# --- the `input` card's text box (vault-record spec §2) ---------------------


def _answer_input(view):
    return next(b for b in view["blocks"] if b.get("block_id") == "answer")


def _submission(view, text):
    """What Slack sends back when the modal `view` is submitted with `text`."""
    return {
        "view": {
            "callback_id": view["callback_id"],
            "private_metadata": view["private_metadata"],
            "state": {"values": {"answer": {"value": {"value": text}}}},
        }
    }


def test_text_modal_uses_the_cards_own_label_and_placeholder():
    view = build_text_modal("ID1", "Who is *Sam*?", "Your answer about Sam", "A name and a role")
    assert view["type"] == "modal"
    assert view["callback_id"] == "text_submit"
    field = _answer_input(view)
    assert field["label"]["text"] == "Your answer about Sam"
    element = field["element"]
    assert element["type"] == "plain_text_input"
    assert element["multiline"] is True
    assert element["max_length"] == TEXT_ANSWER_MAX == 3000
    assert element["placeholder"]["text"] == "A name and a role"
    assert view["blocks"][0]["text"] == {"type": "mrkdwn", "text": "Who is *Sam*?"}


def test_text_modal_falls_back_to_plain_defaults():
    view = build_text_modal("ID1", "", "", "")
    field = _answer_input(view)
    assert field["label"]["text"]
    assert field["element"]["placeholder"]["text"]
    # Slack refuses a section with empty text, so no prompt means no section.
    assert [b["block_id"] for b in view["blocks"]] == ["answer"]


def test_text_modal_cuts_a_long_prompt_to_slacks_limit():
    view = build_text_modal("ID1", "a" * 5000, "", "")
    text = view["blocks"][0]["text"]["text"]
    assert len(text) == 3000 and text.endswith("…")


def test_text_submission_round_trips_the_card_reference():
    view = build_text_modal("ID1", "q", "", "", channel="C9", ts="171.5")
    assert json.loads(view["private_metadata"]) == {"id": "ID1", "channel": "C9", "ts": "171.5"}
    parsed = parse_text_submission(_submission(view, "  Sam runs finance.  "))
    assert parsed is not None
    assert (parsed.interaction_id, parsed.channel, parsed.ts) == ("ID1", "C9", "171.5")
    assert parsed.text == "Sam runs finance."


def test_a_blank_text_submission_parses_to_empty_text():
    view = build_text_modal("ID1", "q", "", "")
    parsed = parse_text_submission(_submission(view, " \n\t "))
    assert parsed is not None and parsed.text == ""


def test_text_submission_ignores_other_modals_and_broken_metadata():
    assert parse_text_submission({"view": {"callback_id": "hint_submit"}}) is None
    assert parse_text_submission(
        {"view": {"callback_id": "text_submit", "private_metadata": "not json"}}
    ) is None
    assert parse_text_submission(
        {"view": {"callback_id": "text_submit", "private_metadata": json.dumps({"id": ""})}}
    ) is None
