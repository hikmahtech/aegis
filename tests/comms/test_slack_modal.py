from aegis_comms.slack_modal import build_hint_modal, parse_view_submission


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
