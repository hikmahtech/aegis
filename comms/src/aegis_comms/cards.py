"""Channel-neutral interaction-card rendering.

`render_slack_blocks` produces the Slack Block Kit blocks for a neutral
`CardSpec` (built by the worker and POSTed to the comms delivery endpoint).

Dispatch matrix:
  approval     — two callback buttons (approve, reject)
  choice       — one callback button per key in `options`
  ack          — one Acknowledge button + optional URL button from `options.url`
                 (supports `{interaction_id}` substitution)
  input        — one URL button linking to AEGIS UI
  draft_review — one URL button linking to AEGIS UI
"""

from __future__ import annotations

import structlog

from aegis_comms.adapters.base import CardSpec
from aegis_comms.format import html_to_mrkdwn

_logger = structlog.get_logger()

# Slack button text is plain_text, max 75 chars.
_SLACK_BUTTON_MAX = 75
# Slack section-block text.text max is 3000 chars.
_SLACK_SECTION_MAX = 3000


def _slack_button(text: str, *, value: str | None = None, url: str | None = None,
                  action_id: str | None = None, style: str | None = None) -> dict:
    """Build a Block Kit button element (value xor url)."""
    btn: dict = {
        "type": "button",
        "text": {"type": "plain_text", "text": text[:_SLACK_BUTTON_MAX], "emoji": True},
    }
    if action_id:
        btn["action_id"] = action_id
    if url is not None:
        btn["url"] = url
    if value is not None:
        btn["value"] = value
    if style:
        btn["style"] = style
    return btn


def render_slack_blocks(spec: CardSpec) -> list[dict]:
    """Render Slack Block Kit blocks for a CardSpec.

    Always emits a leading `section` block with the mrkdwn-converted prompt,
    followed (for button kinds) by an `actions` block. Callback-button identity
    is stable so the resolve route stays identical:
    `value=interaction:{id}:{v}` and `action_id=interaction_{v}`.
    """
    interaction_id = spec.interaction_id
    kind = spec.kind
    options = spec.options or {}

    section_text = html_to_mrkdwn(spec.prompt or "")
    if len(section_text) > _SLACK_SECTION_MAX:
        section_text = section_text[: _SLACK_SECTION_MAX - 1] + "…"
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": section_text}}
    ]

    def _cb(v: str) -> dict:
        return {"value": f"interaction:{interaction_id}:{v}", "action_id": f"interaction_{v}"}

    elements: list[dict] = []
    if kind == "approval":
        elements = [
            _slack_button("✅ Approve", style="primary", **_cb("approve")),
            _slack_button("❌ Reject", style="danger", **_cb("reject")),
        ]
    elif kind == "choice":
        elements = [
            _slack_button(str(label), **_cb(str(key))) for key, label in options.items()
        ]
    elif kind == "ack":
        url_template = options.get("url")
        if url_template:
            url = str(url_template).replace("{interaction_id}", interaction_id)
            button_label = str(options.get("button_label") or "🔗 Open")
            elements.append(_slack_button(button_label, url=url, action_id="open_url"))
        elements.append(_slack_button("✓ Acknowledge", **_cb("ack")))
    elif kind in ("input", "draft_review"):
        base_url = options.get("aegis_ui_url", "")
        if base_url:
            url = f"{str(base_url).rstrip('/')}/interactions/{interaction_id}"
            label = "📝 Open in admin" if kind == "input" else "✏️ Review & send"
            elements.append(_slack_button(label, url=url, action_id="open_url"))
    else:
        # Unknown kind -> section only, warn.
        _logger.warning("interaction_card_unknown_kind", kind=kind)

    if getattr(spec, "allow_hint", False):
        elements.append(
            _slack_button(
                "✏️ Give a hint",
                value=f"interaction:{interaction_id}:hint_open",
                action_id="hint_open",
            )
        )

    if elements:
        if kind in ("approval", "choice", "ack"):
            # Optional free-text note that rides along with whichever button
            # is tapped (Slack includes the message's input-block state in
            # every block_actions payload). A non-empty note lands in
            # response.note, which the core learning loop records as a durable
            # agent_memory lesson — bare taps stay one-click.
            blocks.append(
                {
                    "type": "input",
                    "block_id": "correction_note",
                    "optional": True,
                    "label": {
                        "type": "plain_text",
                        "text": "Why? (optional — teaches the agent)",
                    },
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "note",
                        "placeholder": {
                            "type": "plain_text",
                            "text": "Add a reason and it becomes a durable lesson",
                        },
                    },
                }
            )
        blocks.append({"type": "actions", "elements": elements})
    return blocks
