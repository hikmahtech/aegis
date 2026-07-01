"""Slack hint modal for the Gate-0 repo-confirm card (views.open + view_submission)."""

from __future__ import annotations


def build_hint_modal(interaction_id: str, alert_title: str) -> dict:
    title = (alert_title or "").strip()[:150]
    return {
        "type": "modal",
        "callback_id": "hint_submit",
        "private_metadata": interaction_id,
        "title": {"type": "plain_text", "text": "Give a hint"},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Which repo is this about?*\n{title}"},
            },
            {
                "type": "input",
                "block_id": "hint",
                "label": {"type": "plain_text", "text": "Repo (owner/name) or a keyword"},
                "element": {"type": "plain_text_input", "action_id": "value"},
            },
        ],
    }


def parse_view_submission(payload: dict) -> tuple[str, str] | None:
    view = payload.get("view") or {}
    if view.get("callback_id") != "hint_submit":
        return None
    interaction_id = view.get("private_metadata") or ""
    state = (view.get("state") or {}).get("values") or {}
    text = (((state.get("hint") or {}).get("value") or {}).get("value") or "").strip()
    if not interaction_id or not text:
        return None
    return interaction_id, text
