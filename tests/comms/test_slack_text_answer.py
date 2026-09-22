"""An `input` card answered in a Slack text box (vault-record spec §2, phase 3).

The Answer button opens a modal; submitting it resolves the card with
`{"value": <text>}`, the same shape the admin textarea sends, and edits the
card to say it was answered. Rules checked here that must not loosen:

- a save that is not confirmed never loses the text: the save is tried once,
  within `TEXT_SAVE_BUDGET_S`, BEFORE the ack, and anything but a confirmed
  save keeps the modal open with the text still in it;
- a resend after a first try that timed out but landed reads as answered;
  an answer from somewhere else still reads as closed;
- the card never shows the text, and no log line carries it (spec §18 q5 —
  a diary answer is private). Logs carry the interaction id and the length;
- a blank answer is refused in the modal.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
import structlog
from aegis_comms import slack_inbound
from aegis_comms.adapters.base import DeliveryRef
from aegis_comms.adapters.slack import handle_text_open, handle_text_submit
from aegis_comms.slack_inbound import SlackCoreClient, SlackInbound
from aegis_comms.slack_modal import build_text_modal

# A marker that cannot occur in any log line by chance. Repeated so that any
# truncation (the core client cuts bodies at 200 and 400) would still hold it.
_MARK = "ZQ7PRIVATEDIARY"
_SECRET = (f"{_MARK} I told nobody about the move. " * 12).strip()

_CARD = DeliveryRef("slack", {"channel": "C9", "ts": "171.5"})


class _Settings:
    core_url = "http://core"
    api_key = ""
    admin_username = "admin"
    admin_password = "admin"


class _FakeCore:
    """Core's resolve route in miniature: the first resolve wins and stores the
    response; any later one answers `already_resolved` with the row's status.

    A resolve lands at core BEFORE its reply is sent, so a slow reply that the
    caller gives up on has still resolved the card: that is the late landing.
    """

    def __init__(self, *, status="pending", stored=None, reply_delay=0.0):
        self.status = status
        self.response = stored
        self.reply_delay = reply_delay
        self.get_fails = False
        self.posts = 0
        self.applied = 0

    async def resolve_interaction(self, *, interaction_id, value, note="", error_sink=None):
        self.posts += 1
        already = self.status != "pending"
        if not already:
            self.status, self.response = "resolved", {"value": value}
            self.applied += 1
        delay, self.reply_delay = self.reply_delay, 0.0  # only the first reply is slow
        if delay:
            await asyncio.sleep(delay)
        return {"interaction_id": interaction_id, "status": self.status, "already_resolved": already}

    async def get_interaction(self, interaction_id):
        if self.get_fails:
            return None
        return {"id": interaction_id, "status": self.status, "response": self.response}


def test_the_fake_core_matches_the_real_client():
    """A fake that drifts from the real client tests nothing (see the
    unfalsifiable-test lesson). Pin the two methods the text path calls."""
    for name in ("resolve_interaction", "get_interaction"):
        real = inspect.signature(getattr(SlackCoreClient, name)).parameters
        fake = inspect.signature(getattr(_FakeCore, name)).parameters
        assert list(real) == list(fake), name


def _inbound(core):
    adapter = AsyncMock()
    inbound = SlackInbound(adapter=adapter, core=core, channel_agent_map={}, bot_user_id="U0BOT")
    return inbound, adapter


async def _answer(inbound, text=_SECRET):
    ack = AsyncMock()
    await inbound.on_text_answer(
        interaction_id="ID1", text=text, channel_id="C9", message_ts="171.5", ack=ack
    )
    return ack


def _kept_open(ack) -> str:
    """The modal stayed open: the one ack carried an error. Returns its text."""
    ack.assert_awaited_once()
    kwargs = ack.await_args.kwargs
    assert kwargs["response_action"] == "errors"
    assert set(kwargs["errors"]) == {"answer"}
    assert _MARK not in kwargs["errors"]["answer"]
    return kwargs["errors"]["answer"]


def _closed(ack) -> None:
    ack.assert_awaited_once_with()


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

    _kept_open(ack)
    inbound.on_text_answer.assert_not_awaited()


async def test_the_submit_handler_leaves_the_ack_to_the_save():
    """The handler must not close the modal itself: only a confirmed save may."""
    ack = AsyncMock()
    inbound = AsyncMock()
    await handle_text_submit(ack, inbound, _submit_body("  Sam runs finance. "))

    ack.assert_not_awaited()
    inbound.on_text_answer.assert_awaited_once_with(
        interaction_id="ID1", text="Sam runs finance.", channel_id="C9", message_ts="171.5",
        ack=ack,
    )


async def test_a_submission_that_names_no_card_only_closes_the_modal():
    ack = AsyncMock()
    inbound = AsyncMock()
    await handle_text_submit(ack, inbound, {"view": {"callback_id": "text_submit"}})
    ack.assert_awaited_once_with()
    inbound.on_text_answer.assert_not_awaited()


# --- saving -------------------------------------------------------------------


def test_the_save_budget_leaves_room_inside_slacks_three_seconds():
    assert 0 < slack_inbound.TEXT_SAVE_BUDGET_S <= 2.2


async def test_a_saved_answer_closes_the_modal_and_the_card_never_shows_it():
    core = _FakeCore()
    inbound, adapter = _inbound(core)

    ack = await _answer(inbound)

    _closed(ack)
    assert core.response == {"value": _SECRET}
    adapter.edit_card.assert_awaited_once()
    edit = adapter.edit_card.await_args.kwargs
    assert edit["ref"] == _CARD
    assert edit["text"] == "✅ Answered"
    adapter.post_thread.assert_not_awaited()


@respx.mock
async def test_core_down_keeps_the_modal_open_and_resolves_nothing():
    route = respx.post("http://core/api/interactions/ID1/resolve").mock(
        side_effect=httpx.ConnectError("refused")
    )
    inbound, adapter = _inbound(SlackCoreClient(_Settings()))

    ack = await _answer(inbound)

    assert "Press Send again" in _kept_open(ack)
    # One try only: no retry behind a closed modal.
    assert route.call_count == 1
    adapter.edit_card.assert_not_awaited()
    adapter.post_thread.assert_not_awaited()


async def test_core_slower_than_the_budget_keeps_the_modal_open(monkeypatch):
    monkeypatch.setattr(slack_inbound, "TEXT_SAVE_BUDGET_S", 0.1)

    class _Stuck(_FakeCore):
        async def resolve_interaction(self, *, interaction_id, value, note="", error_sink=None):
            await asyncio.sleep(30)  # never lands

    core = _Stuck()
    inbound, adapter = _inbound(core)

    started = time.monotonic()
    ack = await _answer(inbound)

    assert time.monotonic() - started < 2
    assert "Press Send again" in _kept_open(ack)
    assert core.applied == 0
    adapter.edit_card.assert_not_awaited()


async def test_a_resend_after_a_late_landing_first_try_reads_as_answered(monkeypatch):
    """The first Send reaches core but its reply is too slow, so the modal
    stays open. The second Send finds the card already resolved, by that first
    Send. That is this answer saved, not someone else closing the card."""
    monkeypatch.setattr(slack_inbound, "TEXT_SAVE_BUDGET_S", 0.1)
    core = _FakeCore(reply_delay=30)
    inbound, adapter = _inbound(core)

    first = await _answer(inbound)
    assert "Press Send again" in _kept_open(first)
    adapter.edit_card.assert_not_awaited()

    second = await _answer(inbound)

    _closed(second)
    assert core.applied == 1
    text = adapter.edit_card.await_args.kwargs["text"]
    assert text == "✅ Answered"


async def test_a_card_answered_elsewhere_says_closed_and_keeps_the_text():
    core = _FakeCore(status="resolved", stored={"value": "someone else's words"})
    inbound, adapter = _inbound(core)

    ack = await _answer(inbound)

    assert "not saved" in _kept_open(ack)
    assert core.applied == 0
    text = adapter.edit_card.await_args.kwargs["text"]
    assert "not recorded" in text
    assert _MARK not in text


async def test_an_unreadable_card_after_already_resolved_asks_to_send_again():
    core = _FakeCore(status="resolved", stored={"value": "someone else's words"})
    core.get_fails = True
    inbound, adapter = _inbound(core)

    ack = await _answer(inbound)

    assert "Press Send again" in _kept_open(ack)
    adapter.edit_card.assert_not_awaited()


async def test_an_expired_card_says_so_and_keeps_the_text():
    core = _FakeCore(status="archived")
    inbound, adapter = _inbound(core)

    ack = await _answer(inbound)

    assert "not saved" in _kept_open(ack)
    text = adapter.edit_card.await_args.kwargs["text"]
    assert text.startswith("⏰ Expired")
    assert _MARK not in text


@respx.mock
async def test_a_card_that_no_longer_exists_keeps_the_text():
    respx.post("http://core/api/interactions/ID1/resolve").mock(
        return_value=httpx.Response(404, json={"detail": "interaction_not_found"})
    )
    inbound, adapter = _inbound(SlackCoreClient(_Settings()))

    ack = await _answer(inbound)

    assert "no longer exists" in _kept_open(ack)
    adapter.edit_card.assert_not_awaited()


# --- the privacy rule: the text never reaches a log ---------------------------


@pytest.mark.parametrize("outcome", ["ok", "gone", "unreachable", "late", "elsewhere"])
@respx.mock
async def test_no_log_line_carries_the_answer(outcome, caplog):
    route = respx.post("http://core/api/interactions/ID1/resolve")
    fetched = respx.get("http://core/api/interactions/ID1")
    if outcome == "ok":
        route.mock(return_value=httpx.Response(
            200, json={"interaction_id": "ID1", "status": "resolved", "already_resolved": False}
        ))
    elif outcome == "gone":
        route.mock(return_value=httpx.Response(404, json={"detail": "interaction_not_found"}))
    elif outcome == "unreachable":
        route.mock(side_effect=httpx.ConnectError("refused"))
    else:
        # Already resolved: by this same answer (late) or by another (elsewhere).
        # The GET body holds the stored text, so it must not reach a log either.
        route.mock(return_value=httpx.Response(
            200, json={"interaction_id": "ID1", "status": "resolved", "already_resolved": True}
        ))
        stored = _SECRET if outcome == "late" else f"{_MARK} other words"
        fetched.mock(return_value=httpx.Response(
            200, json={"id": "ID1", "status": "resolved", "response": {"value": stored}}
        ))

    inbound, adapter = _inbound(SlackCoreClient(_Settings()))
    ack = AsyncMock()
    caplog.set_level(logging.DEBUG)
    with structlog.testing.capture_logs() as logs:
        await handle_text_submit(ack, inbound, _submit_body(_SECRET))

    # The real client sent the text to core, as `value` and nothing else.
    sent = json.loads(route.calls[0].request.content)
    assert sent == {"response": {"value": _SECRET}}

    assert _MARK not in repr(logs)
    assert _MARK not in caplog.text
    assert _MARK not in repr(ack.await_args_list)
    for call in adapter.method_calls:
        assert _MARK not in repr(call)
    if outcome == "late":
        _closed(ack)
    if outcome == "elsewhere":
        _kept_open(ack)

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
