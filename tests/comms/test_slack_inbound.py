"""Slack inbound tests — pure routing + SlackInbound.on_* methods.

No live Slack: the SlackCoreClient and SlackAdapter are AsyncMocks, so the
on_* methods are exercised directly without a socket. Mirrors the Telegram
bot's routing + core-call contracts (bot.py::_message / _send_chat /
_dispatch_agent_reply / handle_capture_command / handle_interaction_callback).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from aegis_comms.adapters.base import DeliveryRef, SendResult
from aegis_comms.slack_inbound import (
    SlackCoreClient,
    SlackInbound,
    parse_action,
    route_message,
)

# Channel → agent map used across routing tests (the reverse of the adapter's
# resolve: built from /api/agents slack_channel_id).
_CHANNEL_MAP = {
    "CSEBAS": "sebas",
    "CRAPH": "raphael",
    "CPANDORA": "pandoras-actor",
}


def _settings():
    """Minimal settings stub for SlackCoreClient (reused across tests)."""
    return type("S", (), {
        "core_url": "http://core", "api_key": "",
        "admin_username": "admin", "admin_password": "admin",
    })()


# --- pure routing helpers ---------------------------------------------------


def test_route_message_plain_in_sebas_channel_is_sync():
    mode, agent, text = route_message("CSEBAS", "hello there", _CHANNEL_MAP)
    assert mode == "sync"
    assert agent == "sebas"
    assert text == "hello there"


def test_route_message_pandora_channel_is_async():
    mode, agent, text = route_message("CPANDORA", "check the swarm", _CHANNEL_MAP)
    assert mode == "async"
    assert agent == "pandoras-actor"
    assert text == "check the swarm"


def test_route_message_explicit_mention_is_async_and_strips_mention():
    mode, agent, text = route_message("CSEBAS", "@raphael find the ref", _CHANNEL_MAP)
    assert mode == "async"
    assert agent == "raphael"
    assert "@raphael" not in text
    assert text == "find the ref"


def test_route_message_unknown_channel_defaults_to_sebas_sync():
    # Behavior changed (slice 3): an unmapped channel with no @mention now
    # routes via the front-door intent classifier, not a hardcoded sebas.
    mode, agent, text = route_message("CUNKNOWN", "hi", _CHANNEL_MAP)
    assert mode == "route"
    assert agent == ""


def test_route_message_unbound_channel_returns_route_sentinel():
    # Unmapped channel + no @mention → front-door intent routing.
    mode, agent, text = route_message("CUNKNOWN", "hi there", _CHANNEL_MAP)
    assert mode == "route"
    assert agent == ""
    assert text == "hi there"


def test_route_message_bound_channel_still_sync():
    # A channel bound to an agent is a dedicated persona channel — unchanged.
    mode, agent, text = route_message("CSEBAS", "hello", _CHANNEL_MAP)
    assert mode == "sync"
    assert agent == "sebas"


def test_route_message_unbound_strips_bot_mention():
    mode, agent, text = route_message(
        "CUNKNOWN", "<@U0BOT> what's my bill", _CHANNEL_MAP, mention_bot_id="U0BOT"
    )
    assert mode == "route"
    assert text == "what's my bill"


def test_route_message_app_mention_bot_id_routes_async():
    """When the bot itself is @mentioned (app_mention), route async to the
    channel's agent even without an explicit @agent token."""
    mode, agent, text = route_message(
        "CSEBAS", "<@U0BOT> please summarize", _CHANNEL_MAP, mention_bot_id="U0BOT"
    )
    assert mode == "async"
    assert agent == "sebas"
    # The bot mention is stripped from the text the LLM sees.
    assert "<@U0BOT>" not in text
    assert text == "please summarize"


def test_parse_action_splits_interaction_value():
    assert parse_action("interaction:i9:approve") == ("i9", "approve")


def test_parse_action_value_may_contain_colon():
    # split(":", 2) keeps trailing colons in the value.
    assert parse_action("interaction:i1:option:a") == ("i1", "option:a")


# --- SlackInbound.on_* ------------------------------------------------------


def _inbound(channel_map=None, elevenlabs_api_key=""):
    core = AsyncMock()
    adapter = AsyncMock()
    inbound = SlackInbound(
        adapter=adapter,
        core=core,
        channel_agent_map=channel_map if channel_map is not None else dict(_CHANNEL_MAP),
        bot_user_id="U0BOT",
        bot_token="xoxb-test",
        elevenlabs_api_key=elevenlabs_api_key,
    )
    return inbound, core, adapter


async def test_on_message_sync_path_chats_then_posts_then_patches_ref():
    inbound, core, adapter = _inbound()
    core.chat.return_value = {
        "response": "hi back",
        "assistant_message_id": "row-42",
    }
    # The adapter's reply send returns the REPLY's ref (channel + ts).
    reply_ref = DeliveryRef("slack", {"channel": "CSEBAS", "ts": "9.9"})
    adapter.send_message.return_value = SendResult(ok=True, ref=reply_ref, used_html=False)

    await inbound.on_message(
        channel_id="CSEBAS", text="hello", user_id="UME"
    )

    # 1) core.chat called with the channel's agent + a slack thread id.
    core.chat.assert_awaited_once()
    ckw = core.chat.await_args.kwargs
    assert ckw["agent_id"] == "sebas"
    assert ckw["message"] == "hello"
    assert ckw["thread_id"] == "slack-CSEBAS-sebas"
    assert ckw["delivery_ref"] is None

    # 2) reply posted via the adapter to the same channel.
    adapter.send_message.assert_awaited_once()
    skw = adapter.send_message.await_args.kwargs
    assert skw["agent_id"] == "sebas"
    assert skw["text"] == "hi back"
    assert skw["target"] == {"channel": "CSEBAS"}

    # 3) attach the delivery-ref to the assistant row (the two-step).
    core.attach_delivery_ref.assert_awaited_once()
    pkw = core.attach_delivery_ref.await_args.kwargs
    assert pkw["message_id"] == "row-42"
    assert pkw["delivery_ref"] == reply_ref.to_dict()


async def test_on_message_sync_path_ordering():
    """chat → send_message → attach_delivery_ref, in that order."""
    inbound, core, adapter = _inbound()
    calls: list[str] = []
    core.chat.side_effect = lambda **k: calls.append("chat") or {
        "response": "r",
        "assistant_message_id": "row-1",
    }
    adapter.send_message.side_effect = lambda **k: calls.append("send") or SendResult(
        ok=True, ref=DeliveryRef("slack", {"channel": "CSEBAS", "ts": "2.2"})
    )
    core.attach_delivery_ref.side_effect = lambda **k: calls.append("patch") or {}

    await inbound.on_message(
        channel_id="CSEBAS", text="hi", user_id="UME"
    )
    assert calls == ["chat", "send", "patch"]


async def test_on_message_async_path_triggers_and_acks():
    inbound, core, adapter = _inbound()
    core.agent_reply_trigger.return_value = {"workflow_id": "wf-1"}

    await inbound.on_message(
        channel_id="CPANDORA", text="check swarm", user_id="UME"
    )

    core.agent_reply_trigger.assert_awaited_once()
    tkw = core.agent_reply_trigger.await_args.kwargs
    assert tkw["target_agent"] == "pandoras-actor"
    assert tkw["message"] == "check swarm"
    assert tkw["thread_id"] == "slack-CPANDORA-pandoras-actor"
    assert tkw["reply_chat_id"] == 0

    # No sync chat on the async path when trigger succeeds.
    core.chat.assert_not_awaited()
    # A short routing ack is posted to the channel.
    adapter.send_message.assert_awaited_once()
    akw = adapter.send_message.await_args.kwargs
    assert akw["agent_id"] == "pandoras-actor"
    assert "Routing" in akw["text"]
    assert akw["target"] == {"channel": "CPANDORA"}


async def test_on_message_async_trigger_failure_falls_back_to_sync():
    """Fix 2: async trigger failure → sync fallback (chat called, no bare ack)."""
    inbound, core, adapter = _inbound()
    # Trigger returns None (non-2xx / transport error).
    core.agent_reply_trigger.return_value = None
    core.chat.return_value = {"response": "sync reply", "assistant_message_id": "row-9"}
    adapter.send_message.return_value = SendResult(ok=True, ref=None, used_html=False)

    await inbound.on_message(
        channel_id="CPANDORA", text="check swarm", user_id="UME"
    )

    # Trigger was attempted.
    core.agent_reply_trigger.assert_awaited_once()
    # Fell back to sync: core.chat must have been called.
    core.chat.assert_awaited_once()
    ckw = core.chat.await_args.kwargs
    assert ckw["agent_id"] == "pandoras-actor"
    assert ckw["message"] == "check swarm"
    # The reply was sent (not a bare ack).
    adapter.send_message.assert_awaited_once()
    skw = adapter.send_message.await_args.kwargs
    assert skw["text"] == "sync reply"


async def test_on_message_ignores_bot_own_messages():
    inbound, core, adapter = _inbound()
    # bot_id present (event came from a bot) → ignore.
    await inbound.on_message(
        channel_id="CSEBAS", text="echo", user_id=None, bot_id="B0"
    )
    core.chat.assert_not_awaited()
    core.agent_reply_trigger.assert_not_awaited()
    adapter.send_message.assert_not_awaited()


async def test_on_message_ignores_own_user_id():
    inbound, core, adapter = _inbound()
    # user_id equals our bot user id → ignore (avoids loops).
    await inbound.on_message(
        channel_id="CSEBAS", text="echo", user_id="U0BOT"
    )
    core.chat.assert_not_awaited()
    adapter.send_message.assert_not_awaited()


async def test_on_action_resolves_then_stamps_card():
    inbound, core, adapter = _inbound()
    core.resolve_interaction.return_value = {"status": "resolved"}

    await inbound.on_action(
        value="interaction:i1:approve", channel_id="CSEBAS", message_ts="3.3"
    )

    core.resolve_interaction.assert_awaited_once_with(
        interaction_id="i1", value="approve"
    )
    adapter.edit_card.assert_awaited_once()
    ekw = adapter.edit_card.await_args.kwargs
    assert ekw["ref"] == DeliveryRef("slack", {"channel": "CSEBAS", "ts": "3.3"})
    assert "approve" in ekw["text"]


async def test_on_action_already_resolved_also_stamps_card():
    """Fix 3 (success variant): already_resolved is treated as success."""
    inbound, core, adapter = _inbound()
    core.resolve_interaction.return_value = {"status": "already_resolved"}

    await inbound.on_action(
        value="interaction:i2:reject", channel_id="CSEBAS", message_ts="4.4"
    )

    adapter.edit_card.assert_awaited_once()


async def test_on_action_failed_resolve_does_not_stamp_card():
    """Fix 3: non-resolved result → edit_card NOT called, buttons survive."""
    inbound, core, adapter = _inbound()
    # Simulate persistent transport failure (all 3 attempts return None).
    core.resolve_interaction.return_value = None

    await inbound.on_action(
        value="interaction:i3:approve", channel_id="CSEBAS", message_ts="5.5"
    )

    # Retried 3 times.
    assert core.resolve_interaction.await_count == 3
    # Card must NOT be stamped (buttons left intact for re-click).
    adapter.edit_card.assert_not_awaited()


async def test_on_action_error_status_does_not_stamp_card():
    """Fix 3: a non-resolved status (e.g. 'error') → card not stamped."""
    inbound, core, adapter = _inbound()
    core.resolve_interaction.return_value = {"status": "error", "detail": "timeout"}

    await inbound.on_action(
        value="interaction:i4:approve", channel_id="CSEBAS", message_ts="6.6"
    )

    adapter.edit_card.assert_not_awaited()


# --- SlackCoreClient.attach_delivery_ref POST regression guard --------------


@pytest.mark.asyncio
@respx.mock
async def test_attach_delivery_ref_issues_post_not_patch():
    """Fix 1: attach_delivery_ref must issue POST, not PATCH (route is POST-only)."""

    class _FakeSettings:
        core_url = "http://core"
        api_key = "key123"
        admin_username = "admin"
        admin_password = "pass"

    core = SlackCoreClient(_FakeSettings())
    ref_payload = {"channel": "slack", "ts": "7.7"}

    route = respx.post("http://core/api/chat/messages/msg-99/delivery-ref").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    result = await core.attach_delivery_ref(message_id="msg-99", delivery_ref=ref_payload)

    # The route was called (i.e. it was a POST, not a PATCH).
    assert route.called, "Expected a POST to /api/chat/messages/msg-99/delivery-ref"
    req = route.calls.last.request
    assert req.method == "POST"
    body = json.loads(req.content)
    assert body == {"delivery_ref": ref_payload}
    assert result == {"ok": True}


async def test_on_capture_idempotency_key_shape():
    inbound, core, adapter = _inbound()
    core.capture.return_value = {"task_ref": "T1"}

    await inbound.on_capture(text="buy milk", user_id="UME")

    core.capture.assert_awaited_once()
    ckw = core.capture.await_args.kwargs
    assert ckw["text"] == "buy milk"
    assert ckw["external_id"].startswith("slack:UME:")
    # 16 hex chars after the prefix.
    suffix = ckw["external_id"].split(":", 2)[2]
    assert len(suffix) == 16


async def test_on_capture_blank_text_does_not_call_core():
    inbound, core, adapter = _inbound()
    await inbound.on_capture(text="   ", user_id="UME")
    core.capture.assert_not_awaited()


async def test_on_status_summarizes_health_and_agents():
    inbound, core, adapter = _inbound()
    core.health.return_value = {"status": "ok"}
    core.agents.return_value = [{"name": "Sebas"}, {"name": "Maou"}]
    summary = await inbound.on_status()
    assert "ok" in summary
    assert "Sebas" in summary
    assert "Maou" in summary


# --- front-door intent routing (slice 3) ------------------------------------


@pytest.mark.asyncio
async def test_route_intent_returns_agent_on_200():
    client = SlackCoreClient(_settings())
    with respx.mock:
        respx.post("http://core/api/chat/route").mock(
            return_value=httpx.Response(200, json={"agent_id": "maou", "method": "keyword"})
        )
        result = await client.route_intent(message="pay the bill")
    assert result["agent_id"] == "maou"
    assert result["method"] == "keyword"


@pytest.mark.asyncio
async def test_route_intent_defaults_sebas_on_failure():
    client = SlackCoreClient(_settings())
    with respx.mock:
        respx.post("http://core/api/chat/route").mock(return_value=httpx.Response(500))
        result = await client.route_intent(message="anything")
    assert result["agent_id"] == "sebas"
    assert result["method"] == "default"


@pytest.mark.asyncio
async def test_on_message_route_dispatches_as_classified_agent():
    adapter = AsyncMock()
    adapter.send_message = AsyncMock(return_value=SendResult(ok=True, ref=None))
    core = AsyncMock(spec=SlackCoreClient)
    core.route_intent = AsyncMock(return_value={"agent_id": "maou", "method": "keyword"})
    core.chat = AsyncMock(return_value={"response": "hi", "assistant_message_id": None})
    inbound = SlackInbound(adapter=adapter, core=core, channel_agent_map={}, bot_user_id="U0BOT")
    await inbound.on_message(channel_id="CGEN", text="what's my bill", user_id="U1")
    core.route_intent.assert_awaited_once()
    assert core.chat.await_args.kwargs["agent_id"] == "maou"


# --- conversation stickiness (slice 4) ----------------------------------------


def test_sticky_get_set_and_ttl():
    from aegis_comms.slack_inbound import _STICKY_TTL_SECONDS
    inbound = SlackInbound(adapter=AsyncMock(), core=AsyncMock(), channel_agent_map={})
    assert inbound._sticky_get("C", now=100.0) is None
    inbound._sticky_set("C", "maou", now=100.0)
    assert inbound._sticky_get("C", now=200.0) == "maou"                     # fresh
    assert inbound._sticky_get("C", now=100.0 + _STICKY_TTL_SECONDS + 1) is None  # expired


@pytest.mark.asyncio
async def test_ambiguous_followup_sticks_to_prior_agent():
    adapter = AsyncMock()
    adapter.send_message = AsyncMock(return_value=SendResult(ok=True, ref=None))
    core = AsyncMock(spec=SlackCoreClient)
    core.chat = AsyncMock(return_value={"response": "ok", "assistant_message_id": None})
    inbound = SlackInbound(adapter=adapter, core=core, channel_agent_map={}, bot_user_id="U0BOT")
    # turn 1: clear keyword → maou (sets sticky)
    core.route_intent = AsyncMock(return_value={"agent_id": "maou", "method": "keyword"})
    await inbound.on_message(channel_id="CGEN", text="what's my electricity bill", user_id="U1")
    assert core.chat.await_args.kwargs["agent_id"] == "maou"
    # turn 2: ambiguous (llm) → STAYS maou despite classifier saying sebas
    core.route_intent = AsyncMock(return_value={"agent_id": "sebas", "method": "llm"})
    await inbound.on_message(channel_id="CGEN", text="and last month?", user_id="U1")
    assert core.chat.await_args.kwargs["agent_id"] == "maou"


@pytest.mark.asyncio
async def test_keyword_overrides_sticky():
    adapter = AsyncMock()
    adapter.send_message = AsyncMock(return_value=SendResult(ok=True, ref=None))
    core = AsyncMock(spec=SlackCoreClient)
    core.chat = AsyncMock(return_value={"response": "ok", "assistant_message_id": None})
    core.agent_reply_trigger = AsyncMock(return_value={"workflow_id": "x"})
    inbound = SlackInbound(adapter=adapter, core=core, channel_agent_map={}, bot_user_id="U0BOT")
    core.route_intent = AsyncMock(return_value={"agent_id": "maou", "method": "keyword"})
    await inbound.on_message(channel_id="CGEN", text="my bill", user_id="U1")  # sticky=maou
    # clear keyword for pandora → overrides sticky (and pandora → async)
    core.route_intent = AsyncMock(return_value={"agent_id": "pandoras-actor", "method": "keyword"})
    await inbound.on_message(channel_id="CGEN", text="restart the server", user_id="U1")
    assert core.agent_reply_trigger.await_args.kwargs["target_agent"] == "pandoras-actor"


@pytest.mark.asyncio
async def test_mention_seeds_sticky_for_followup():
    adapter = AsyncMock()
    adapter.send_message = AsyncMock(return_value=SendResult(ok=True, ref=None))
    core = AsyncMock(spec=SlackCoreClient)
    core.agent_reply_trigger = AsyncMock(return_value={"workflow_id": "x"})
    core.chat = AsyncMock(return_value={"response": "ok", "assistant_message_id": None})
    inbound = SlackInbound(adapter=adapter, core=core, channel_agent_map={}, bot_user_id="U0BOT")
    # turn 1: explicit @maou (route_message → async maou) seeds sticky=maou
    await inbound.on_message(channel_id="CGEN", text="@maou what's owed", user_id="U1")
    # turn 2: ambiguous follow-up → sticks to maou
    core.route_intent = AsyncMock(return_value={"agent_id": "sebas", "method": "default"})
    await inbound.on_message(channel_id="CGEN", text="and the total?", user_id="U1")
    assert core.chat.await_args.kwargs["agent_id"] == "maou"


# --- on_file audio (voice notes) → STT → route ------------------------------


def _audio_client(
    *, name="voice.m4a", url="https://files.slack.test/voice.m4a", mimetype="audio/mp4"
):
    """A bolt AsyncWebClient stub whose files_info returns an audio file."""
    client = AsyncMock()
    client.files_info.return_value = {
        "file": {"name": name, "url_private": url, "mimetype": mimetype}
    }
    return client


@respx.mock
async def test_on_file_audio_transcribes_and_routes_in_bound_channel():
    inbound, core, adapter = _inbound(elevenlabs_api_key="el-key")
    core.chat.return_value = {"response": "got it", "assistant_message_id": None}
    adapter.send_message.return_value = SendResult(ok=True, ref=None, used_html=False)

    download = respx.get("https://files.slack.test/voice.m4a").mock(
        return_value=httpx.Response(200, content=b"audio-bytes")
    )
    stt = respx.post("https://api.elevenlabs.io/v1/speech-to-text").mock(
        return_value=httpx.Response(
            200, json={"text": "remind me to call bob", "language_code": "en"}
        )
    )

    await inbound.on_file(file_id="F1", channel_id="CSEBAS", caption="", client=_audio_client())

    assert download.called
    assert stt.called
    assert stt.calls.last.request.headers["xi-api-key"] == "el-key"
    # Transcript routed as a normal typed message → sync chat with the channel's agent.
    core.chat.assert_awaited_once()
    assert core.chat.await_args.kwargs["message"] == "remind me to call bob"
    assert core.chat.await_args.kwargs["agent_id"] == "sebas"
    # The "heard" echo was posted so STT mishears are visible.
    echoed = any(
        "🎤" in (c.kwargs.get("text") or "") for c in adapter.send_message.await_args_list
    )
    assert echoed


@respx.mock
async def test_on_file_audio_ignored_in_unbound_channel():
    inbound, core, adapter = _inbound(elevenlabs_api_key="el-key")

    await inbound.on_file(file_id="F1", channel_id="CUNBOUND", caption="", client=_audio_client())

    # Bound-channels-only: no download, no STT, no routing.
    assert respx.calls.call_count == 0
    core.chat.assert_not_awaited()
    adapter.send_message.assert_not_awaited()


async def test_on_file_audio_without_key_warns_and_skips():
    inbound, core, adapter = _inbound(elevenlabs_api_key="")

    await inbound.on_file(file_id="F1", channel_id="CSEBAS", caption="", client=_audio_client())

    core.chat.assert_not_awaited()
    adapter.send_message.assert_awaited_once()
    assert "ElevenLabs" in adapter.send_message.await_args.kwargs["text"]
