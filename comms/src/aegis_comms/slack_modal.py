"""Slack modals opened from interaction cards (views.open + view_submission).

Two modals live here:

- the hint modal for the Gate-0 repo-confirm card (`hint_submit`);
- the text box for an `input` card (`text_submit`), so an open question is
  answered in Slack instead of only on the admin page.
"""

from __future__ import annotations

import json
from typing import NamedTuple

# Slack's own cap on a plain_text_input.
TEXT_ANSWER_MAX = 3000
# Slack caps a section's text at 3000 characters and a placeholder at 150.
_SECTION_MAX = 3000
_PLACEHOLDER_MAX = 150
_LABEL_MAX = 2000

_DEFAULT_LABEL = "Your answer"
_DEFAULT_PLACEHOLDER = "Type your answer here"


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


def build_text_modal(
    interaction_id: str,
    prompt: str,
    label: str,
    placeholder: str,
    *,
    channel: str = "",
    ts: str = "",
) -> dict:
    """The text box for an `input` card.

    `channel` and `ts` name the card's message. A view_submission payload does
    not say which message opened the modal, so they ride in
    `private_metadata` and let submit edit the card afterwards.
    """
    prompt = (prompt or "").strip()
    if len(prompt) > _SECTION_MAX:
        prompt = prompt[: _SECTION_MAX - 1] + "…"
    blocks: list[dict] = []
    if prompt:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": prompt}})
    blocks.append(
        {
            "type": "input",
            "block_id": "answer",
            "label": {
                "type": "plain_text",
                "text": ((label or "").strip() or _DEFAULT_LABEL)[:_LABEL_MAX],
            },
            "element": {
                "type": "plain_text_input",
                "action_id": "value",
                "multiline": True,
                "max_length": TEXT_ANSWER_MAX,
                "placeholder": {
                    "type": "plain_text",
                    "text": ((placeholder or "").strip() or _DEFAULT_PLACEHOLDER)[
                        :_PLACEHOLDER_MAX
                    ],
                },
            },
        }
    )
    return {
        "type": "modal",
        "callback_id": "text_submit",
        "private_metadata": json.dumps({"id": interaction_id, "channel": channel, "ts": ts}),
        "title": {"type": "plain_text", "text": "Answer"},
        "submit": {"type": "plain_text", "text": "Send"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }


class TextAnswer(NamedTuple):
    """A submitted text box: which card, which message, and what was typed."""

    interaction_id: str
    channel: str
    ts: str
    text: str


def parse_text_submission(payload: dict) -> TextAnswer | None:
    """Read a `text_submit` view_submission.

    None means the payload is not ours, or names no card. Blank text comes back
    as `""` rather than None, so the caller can refuse it in the modal.
    """
    view = payload.get("view") or {}
    if view.get("callback_id") != "text_submit":
        return None
    try:
        meta = json.loads(view.get("private_metadata") or "")
    except ValueError:
        return None
    if not isinstance(meta, dict) or not meta.get("id"):
        return None
    state = (view.get("state") or {}).get("values") or {}
    text = (((state.get("answer") or {}).get("value") or {}).get("value") or "").strip()
    return TextAnswer(
        interaction_id=str(meta["id"]),
        channel=str(meta.get("channel") or ""),
        ts=str(meta.get("ts") or ""),
        text=text,
    )
