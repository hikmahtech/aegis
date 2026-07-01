from aegis_comms.adapters.base import CardSpec
from aegis_comms.cards import render_slack_blocks


def _c(kind, options=None, prompt="p"):
    return CardSpec(
        interaction_id="i1", agent_id="sebas", kind=kind, prompt=prompt, options=options
    )


def _section(blocks):
    return blocks[0]


def _actions(blocks):
    return next(b for b in blocks if b["type"] == "actions")


def test_section_block_first_and_mrkdwn():
    blocks = render_slack_blocks(_c("approval", prompt="<b>ok?</b>"))
    sec = _section(blocks)
    assert sec["type"] == "section"
    assert sec["text"] == {"type": "mrkdwn", "text": "*ok?*"}


def test_approval_buttons():
    blocks = render_slack_blocks(_c("approval"))
    elements = _actions(blocks)["elements"]
    assert elements[0]["text"]["type"] == "plain_text"
    assert elements[0]["value"] == "interaction:i1:approve"
    assert elements[0]["action_id"] == "interaction_approve"
    assert elements[0]["style"] == "primary"
    assert elements[1]["value"] == "interaction:i1:reject"
    assert elements[1]["action_id"] == "interaction_reject"
    assert elements[1]["style"] == "danger"


def test_choice_one_button_per_option():
    blocks = render_slack_blocks(_c("choice", {"k1": "Go", "k2": "Stop"}))
    elements = _actions(blocks)["elements"]
    assert [e["value"] for e in elements] == [
        "interaction:i1:k1",
        "interaction:i1:k2",
    ]
    assert [e["action_id"] for e in elements] == [
        "interaction_k1",
        "interaction_k2",
    ]
    assert [e["text"]["text"] for e in elements] == ["Go", "Stop"]


def test_choice_button_text_truncated_to_75():
    long_label = "x" * 200
    blocks = render_slack_blocks(_c("choice", {"k1": long_label}))
    el = _actions(blocks)["elements"][0]
    assert len(el["text"]["text"]) == 75


def test_ack_url_and_acknowledge():
    blocks = render_slack_blocks(
        _c("ack", {"url": "https://x/{interaction_id}", "button_label": "Open it"})
    )
    elements = _actions(blocks)["elements"]
    url_btn = elements[0]
    assert url_btn["url"] == "https://x/i1"
    assert url_btn["text"]["text"] == "Open it"
    assert "value" not in url_btn
    ack_btn = elements[1]
    assert ack_btn["value"] == "interaction:i1:ack"
    assert ack_btn["action_id"] == "interaction_ack"


def test_ack_default_button_label_and_no_url():
    blocks = render_slack_blocks(_c("ack", None))
    elements = _actions(blocks)["elements"]
    # No url option -> only the Acknowledge button.
    assert len(elements) == 1
    assert elements[0]["value"] == "interaction:i1:ack"


def test_input_url_button():
    blocks = render_slack_blocks(_c("input", {"aegis_ui_url": "https://ui/"}))
    el = _actions(blocks)["elements"][0]
    assert el["url"] == "https://ui/interactions/i1"
    assert el["text"]["text"] == "📝 Open in admin"
    assert "value" not in el


def test_draft_review_url_button():
    blocks = render_slack_blocks(_c("draft_review", {"aegis_ui_url": "https://ui"}))
    el = _actions(blocks)["elements"][0]
    assert el["url"] == "https://ui/interactions/i1"
    assert el["text"]["text"] == "✏️ Review & send"


def test_input_without_ui_url_has_no_actions_block():
    blocks = render_slack_blocks(_c("input", None))
    assert all(b["type"] != "actions" for b in blocks)
    assert blocks[0]["type"] == "section"


def test_unknown_kind_section_only():
    blocks = render_slack_blocks(_c("nope"))
    assert len(blocks) == 1
    assert blocks[0]["type"] == "section"


def test_section_text_truncated_to_3000_chars():
    """A 5000-char prompt must be cut to ≤3000 and end with '…'."""
    long_prompt = "a" * 5000
    blocks = render_slack_blocks(_c("approval", prompt=long_prompt))
    text = _section(blocks)["text"]["text"]
    assert len(text) <= 3000
    assert text.endswith("…")


def test_section_text_short_prompt_unchanged():
    """A prompt under 3000 chars must pass through without truncation."""
    prompt = "hello world"
    blocks = render_slack_blocks(_c("approval", prompt=prompt))
    text = _section(blocks)["text"]["text"]
    assert text == prompt
