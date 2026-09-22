"""An `input` card answered in a Slack text box (vault-record spec §2, phase 3).

The Answer button opens a modal; submitting it resolves the card with
`{"value": <text>}`, the same shape the admin textarea sends, and edits the
card to say it was answered. Two rules are checked here and must not loosen:

- the card never shows the text, and no log line carries it (spec §18 q5 —
  a diary answer is private). Logs carry the interaction id and the length;
- a blank answer is refused in the modal, and a card that is already closed
  is reported closed rather than answered twice.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
import structlog
from aegis_comms.adapters.base import DeliveryRef
from aegis_comms.adapters.slack import handle_text_open, handle_text_submit
from aegis_comms.slack_inbound import SlackCoreClient, SlackInbound
from aegis_comms.slack_modal import build_text_modal

# A marker that cannot occur in any log line by chance. Repeated so that any
# truncation (the core client cuts bodies at 200 and 400) would still hold it.
_MARK = "ZQ7PRIVATEDIARY"
_SECRET = (f"{_MARK} I told nobody about the move. " * 12).strip()


class _Settings:
    core_url = "http://core"
    api_key = ""
    admin_username = "admin"
    admin_password = "admin"


def _inbound(core=None):
    adapter = AsyncMock()
    inbound = SlackInbound(
        adapter=adapter,
        core=core if core is not None else AsyncMock(),
        channel_agent_map={},
        bot_user_id="U0BOT",
    )
    return inbound, adapter


def _submit_body(text, *, interaction_id="ID1", channel="C9", ts="171.5"):
    view = build_text_modal(interaction_id, "q", "", "", channel=channel, ts=ts)
    return {
        "view": {
            "callback_id": view["callback_id"],
            "private_metadata": view["private_metadata"],
            "state": {"values": {"answer": {"value": {"value": text}}}},
        }
    }


# --- opening the modal ------------------------------------------------------


class _FakeClient:
    def __init__(self):
        self.opened = None

    async def views_open(self, *, trigger_id, view):
        self.opened = (trigger_id, view)


async def test_text_open_builds_the_modal_from_the_button_and_the_message():
    client = _FakeClient()
    value = json.dumps({"id": "ID1", "label": "Who is Sam?", "placeholder": "A name"})
    body = {
        "trigger_id": "T1",
        "actions": [{"action_id": "text_open", "value": value}],
        "channel": {"id": "C9"},
        "message": {"ts": "171.5", "text": "Who is *Sam*?"},
    }
    await handle_text_open(client, body)

    assert client.opened is not None
    trigger_id, view = client.opened
    assert trigger_id == "T1"
    assert view["callback_id"] == "text_submit"
    assert json.loads(view["private_metadata"]) == {"id": "ID1", "channel": "C9", "ts": "171.5"}
    answer = next(b for b in view["blocks"] if b.get("block_id") == "answer")
    assert answer["label"]["text"] == "Who is Sam?"
    assert answer["element"]["placeholder"]["text"] == "A name"
    assert view["blocks"][0]["text"]["text"] == "Who is *Sam*?"


async def test_text_open_with_a_broken_button_value_opens_nothing():
    client = _FakeClient()
    for value in ("interaction:ID1:text_open", json.dumps({"label": "no id"}), ""):
        await handle_text_open(client, {"trigger_id": "T1", "actions": [{"value": value}]})
    assert client.opened is None


# --- submitting it ------------------------------------------------------------


async def test_a_blank_answer_is_refused_in_the_modal_and_resolves_nothing():
    ack = AsyncMock()
    inbound = AsyncMock()
    await handle_text_submit(ack, inbound, _submit_body("  \n\t "))

    ack.assert_awaited_once()
    kwargs = ack.await_args.kwargs
    assert kwargs["response_action"] == "errors"
    assert set(kwargs["errors"]) == {"answer"}
    inbound.on_text_answer.assert_not_awaited()


async def test_an_answer_closes_the_modal_first_then_resolves_the_card():
    calls: list[str] = []
    ack = AsyncMock(side_effect=lambda *a, **k: calls.append("ack"))
    inbound = AsyncMock()
    inbound.on_text_answer.side_effect = lambda **k: calls.append("resolve")

    await handle_text_submit(ack, inbound, _submit_body("  Sam runs finance. "))

    # Slack wants the ack inside 3 seconds; the call to core comes after it.
    assert calls == ["ack", "resolve"]
    assert ack.await_args.kwargs == {}
    inbound.on_text_answer.assert_awaited_once_with(
        interaction_id="ID1", text="Sam runs finance.", channel_id="C9", message_ts="171.5"
    )


async def test_a_submission_that_names_no_card_only_closes_the_modal():
    ack = AsyncMock()
    inbound = AsyncMock()
    await handle_text_submit(ack, inbound, {"view": {"callback_id": "text_submit"}})
    ack.assert_awaited_once_with()
    inbound.on_text_answer.assert_not_awaited()


# --- resolving the card -------------------------------------------------------


async def test_the_answer_is_stored_as_value_and_the_card_never_shows_it():
    core = AsyncMock()
    core.resolve_interaction.return_value = {"status": "resolved", "already_resolved": False}
    inbound, adapter = _inbound(core)

    await inbound.on_text_answer(
        interaction_id="ID1", text=_SECRET, channel_id="C9", message_ts="171.5"
    )

    core.resolve_interaction.assert_awaited_once()
    kwargs = core.resolve_interaction.await_args.kwargs
    assert kwargs["interaction_id"] == "ID1"
    assert kwargs["value"] == _SECRET
    assert kwargs["note"] == ""
    adapter.edit_card.assert_awaited_once()
    edit = adapter.edit_card.await_args.kwargs
    assert edit["ref"] == DeliveryRef("slack", {"channel": "C9", "ts": "171.5"})
    assert "Answered" in edit["text"]
    assert _MARK not in edit["text"]
    adapter.post_thread.assert_not_awaited()


async def test_a_card_answered_elsewhere_first_is_reported_closed():
    """Core's `AND status='pending'` guard already stops a second resolve;
    the card must also not claim THIS answer was recorded."""
    core = AsyncMock()
    core.resolve_interaction.return_value = {"status": "resolved", "already_resolved": True}
    inbound, adapter = _inbound(core)

    await inbound.on_text_answer(
        interaction_id="ID1", text=_SECRET, channel_id="C9", message_ts="171.5"
    )

    assert core.resolve_interaction.await_count == 1
    text = adapter.edit_card.await_args.kwargs["text"]
    assert "not recorded" in text
    assert _MARK not in text


async def test_already_resolved_after_a_retry_is_our_own_answer_landing():
    """A transport failure can hide a resolve that did reach core. The retry
    then reads `already_resolved`, and that answer is ours: say Answered."""
    core = AsyncMock()
    core.resolve_interaction.side_effect = [
        None,
        {"status": "resolved", "already_resolved": True},
    ]
    inbound, adapter = _inbound(core)

    await inbound.on_text_answer(
        interaction_id="ID1", text="yes", channel_id="C9", message_ts="171.5"
    )

    assert core.resolve_interaction.await_count == 2
    text = adapter.edit_card.await_args.kwargs["text"]
    assert "Answered" in text and "not recorded" not in text


async def test_an_expired_card_says_so_and_does_not_claim_an_answer():
    core = AsyncMock()
    core.resolve_interaction.return_value = {"status": "archived", "already_resolved": True}
    inbound, adapter = _inbound(core)

    await inbound.on_text_answer(
        interaction_id="ID1", text=_SECRET, channel_id="C9", message_ts="171.5"
    )

    text = adapter.edit_card.await_args.kwargs["text"]
    assert text.startswith("⏰ Expired")
    assert _MARK not in text


# --- the privacy rule: the text never reaches a log ---------------------------


def _resolve_url(interaction_id="ID1"):
    return f"http://core/api/interactions/{interaction_id}/resolve"


@pytest.mark.parametrize("outcome", ["ok", "gone", "unreachable"])
@respx.mock
async def test_no_log_line_carries_the_answer(outcome, caplog):
    route = respx.post(_resolve_url())
    if outcome == "ok":
        route.mock(
            return_value=httpx.Response(
                200, json={"interaction_id": "ID1", "status": "resolved", "already_resolved": False}
            )
        )
    elif outcome == "gone":
        route.mock(return_value=httpx.Response(404, json={"detail": "interaction_not_found"}))
    else:
        route.mock(side_effect=httpx.ConnectError("refused"))

    inbound, adapter = _inbound(SlackCoreClient(_Settings()))
    caplog.set_level(logging.DEBUG)
    with structlog.testing.capture_logs() as logs:
        await handle_text_submit(AsyncMock(), inbound, _submit_body(_SECRET))

    # The real client sent the text to core, as `value` and nothing else.
    sent = json.loads(route.calls[0].request.content)
    assert sent == {"response": {"value": _SECRET}}

    assert _MARK not in repr(logs)
    assert _MARK not in caplog.text
    for call in adapter.method_calls:
        assert _MARK not in repr(call)

    # What is logged instead: the card and how long the answer was.
    submitted = [e for e in logs if e["event"] == "slack_text_answer_submitted"]
    assert submitted == [
        {
            "event": "slack_text_answer_submitted",
            "interaction_id": "ID1",
            "length": len(_SECRET),
            "log_level": "info",
        }
    ]
