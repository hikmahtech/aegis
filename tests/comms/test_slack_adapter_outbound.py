"""SlackAdapter outbound tests — AsyncWebClient is fully mocked; no live Slack.

The core `/api/agents/{id}` lookup is stubbed by monkeypatching `_resolve`
(channel/persona resolution itself is exercised in test_resolve_* below via
respx). `chat_postMessage` returns the standard {ok,ts,channel} shape.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import respx
from aegis_comms.adapters.base import CardSpec, DeliveryRef
from aegis_comms.adapters.slack import SlackAdapter
from aegis_comms.config import CommsSettings
from slack_sdk.errors import SlackApiError


def _settings(**over) -> CommsSettings:
    base = {
        "AEGIS_CHANNEL": "slack",
        "AEGIS_SLACK_BOT_TOKEN": "xoxb-test",
        "AEGIS_CORE_URL": "http://core.test",
        "AEGIS_API_KEY": "k",
    }
    base.update(over)
    return CommsSettings(**base)


def _adapter(
    monkeypatch, *, channel="C1", username="Sebas", icon=":bust_in_silhouette:", voice_id=""
):
    a = SlackAdapter(_settings())
    a._client = AsyncMock()
    a._client.chat_postMessage.return_value = {"ok": True, "ts": "1.2", "channel": channel}
    a._client.chat_update.return_value = {"ok": True, "ts": "1.2"}
    a._client.chat_delete.return_value = {"ok": True}

    async def _fake_resolve(agent_id):
        return (channel, username, icon, voice_id)

    monkeypatch.setattr(a, "_resolve", _fake_resolve)
    return a


def test_name_is_slack():
    assert SlackAdapter(_settings()).name == "slack"


async def test_send_message_posts_with_persona_and_mrkdwn(monkeypatch):
    a = _adapter(monkeypatch)
    r = await a.send_message(agent_id="sebas", text="<b>hi</b> & bye")
    a._client.chat_postMessage.assert_awaited_once()
    kwargs = a._client.chat_postMessage.await_args.kwargs
    assert kwargs["channel"] == "C1"
    assert kwargs["text"] == "*hi* & bye"
    assert kwargs["mrkdwn"] is True
    assert kwargs["username"] == "Sebas"
    assert kwargs["icon_emoji"] == ":bust_in_silhouette:"
    assert r.ok is True
    assert r.used_html is False
    assert r.ref.to_dict() == {"adapter": "slack", "channel": "C1", "ts": "1.2"}


async def test_send_message_target_overrides_channel(monkeypatch):
    a = _adapter(monkeypatch)
    await a.send_message(agent_id="sebas", text="hi", target={"channel": "COVERRIDE"})
    kwargs = a._client.chat_postMessage.await_args.kwargs
    assert kwargs["channel"] == "COVERRIDE"


async def test_send_message_chunks_long_text(monkeypatch):
    a = _adapter(monkeypatch)
    body = "line\n" * 1000  # 5000 chars, > 2800
    r = await a.send_message(agent_id="sebas", text=body)
    assert a._client.chat_postMessage.await_count == 2
    # ref uses the FIRST chunk's ts.
    assert r.ref.data["ts"] == "1.2"
    # All chunks bounded.
    for call in a._client.chat_postMessage.await_args_list:
        assert len(call.kwargs["text"]) <= 2800


async def test_send_message_slack_error_returns_not_ok(monkeypatch):
    a = _adapter(monkeypatch)
    a._client.chat_postMessage.side_effect = SlackApiError(
        "boom", response={"ok": False, "error": "channel_not_found"}
    )
    r = await a.send_message(agent_id="sebas", text="hi")
    assert r.ok is False
    assert r.error


async def test_send_card_passes_blocks_with_callback_value(monkeypatch):
    a = _adapter(monkeypatch)
    spec = CardSpec("i1", "sebas", "approval", "ok?", None)
    r = await a.send_card(spec)
    kwargs = a._client.chat_postMessage.await_args.kwargs
    blocks = kwargs["blocks"]
    actions = next(b for b in blocks if b["type"] == "actions")
    values = [e["value"] for e in actions["elements"]]
    assert "interaction:i1:approve" in values
    assert kwargs["text"]  # fallback text present
    assert r.ok is True
    assert r.ref.data == {"channel": "C1", "ts": "1.2"}


async def test_edit_card_clears_buttons(monkeypatch):
    a = _adapter(monkeypatch)
    ref = DeliveryRef("slack", {"channel": "C1", "ts": "1.2"})
    await a.edit_card(ref=ref, text="<b>done</b>")
    kwargs = a._client.chat_update.await_args.kwargs
    assert kwargs["channel"] == "C1"
    assert kwargs["ts"] == "1.2"
    assert kwargs["text"] == "*done*"
    assert kwargs["blocks"] == []


async def test_delete_message_ok(monkeypatch):
    a = _adapter(monkeypatch)
    ref = DeliveryRef("slack", {"channel": "C1", "ts": "1.2"})
    ok = await a.delete_message(ref=ref)
    a._client.chat_delete.assert_awaited_once_with(channel="C1", ts="1.2")
    assert ok is True


async def test_delete_message_not_found_is_tolerated(monkeypatch):
    a = _adapter(monkeypatch)
    a._client.chat_delete.side_effect = SlackApiError(
        "gone", response={"ok": False, "error": "message_not_found"}
    )
    ref = DeliveryRef("slack", {"channel": "C1", "ts": "1.2"})
    assert await a.delete_message(ref=ref) is True


async def test_delete_message_other_error_is_false(monkeypatch):
    a = _adapter(monkeypatch)
    a._client.chat_delete.side_effect = SlackApiError(
        "nope", response={"ok": False, "error": "cant_delete_message"}
    )
    ref = DeliveryRef("slack", {"channel": "C1", "ts": "1.2"})
    assert await a.delete_message(ref=ref) is False


async def test_send_document_uploads_each_file(monkeypatch):
    a = _adapter(monkeypatch)
    a._client.files_upload_v2 = AsyncMock(
        return_value={"ok": True, "files": [{"id": "F1"}]}
    )
    docs = [
        {"filename": "a.md", "content": "aaa"},
        {"filename": "b.md", "content": "bbb"},
    ]
    r = await a.send_document(agent_id="sebas", documents=docs, caption="cap")
    assert a._client.files_upload_v2.await_count == 2
    first = a._client.files_upload_v2.await_args_list[0].kwargs
    assert first["channel"] == "C1"
    assert first["content"] == "aaa"
    assert first["filename"] == "a.md"
    assert first["initial_comment"] == "cap"
    second = a._client.files_upload_v2.await_args_list[1].kwargs
    assert second["initial_comment"] is None
    assert r.ok is True


async def test_send_card_unknown_kind_still_posts_section(monkeypatch):
    a = _adapter(monkeypatch)
    spec = CardSpec("i1", "sebas", "weird", "hello", None)
    r = await a.send_card(spec)
    kwargs = a._client.chat_postMessage.await_args.kwargs
    assert kwargs["blocks"][0]["type"] == "section"
    assert r.ok is True


@respx.mock
async def test_build_channel_agent_map_reverses_resolve():
    """The inbound channel→agent map is the reverse of _resolve: built from
    /api/agents slack_channel_id, skipping agents without one."""
    respx.get("http://core.test/api/agents").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": "sebas", "slack_channel_id": "CSEBAS"},
                {"id": "pandoras-actor", "slack_channel_id": "CPANDORA"},
                {"id": "maou", "slack_channel_id": None},  # skipped
            ],
        )
    )
    a = SlackAdapter(_settings())
    m = await a._build_channel_agent_map()
    assert m == {"CSEBAS": "sebas", "CPANDORA": "pandoras-actor"}
    await a.stop()


@respx.mock
async def test_build_channel_agent_map_empty_on_fetch_error():
    respx.get("http://core.test/api/agents").mock(side_effect=httpx.NetworkError("down"))
    a = SlackAdapter(_settings())
    assert await a._build_channel_agent_map() == {}
    await a.stop()


# --- channel/persona resolution via the core lookup (respx) ---


@respx.mock
async def test_resolve_uses_core_slack_channel_id():
    respx.get("http://core.test/api/agents/sebas").mock(
        return_value=httpx.Response(
            200, json={"id": "sebas", "name": "Sebas", "slack_channel_id": "CSEBAS"}
        )
    )
    a = SlackAdapter(_settings())
    channel, username, icon, voice_id = await a._resolve("sebas")
    assert channel == "CSEBAS"
    assert username == "Sebas"
    assert icon == ":bust_in_silhouette:"
    assert voice_id == ""
    await a.stop()


@respx.mock
async def test_resolve_caches_core_lookup():
    route = respx.get("http://core.test/api/agents/raphael").mock(
        return_value=httpx.Response(
            200, json={"id": "raphael", "name": "Raphael", "slack_channel_id": "CRAPH"}
        )
    )
    a = SlackAdapter(_settings())
    await a._resolve("raphael")
    await a._resolve("raphael")
    assert route.call_count == 1  # second hit served from cache
    await a.stop()


@respx.mock
async def test_resolve_transient_failure_does_not_poison_cache():
    """First call: core fetch raises → channel=None, cache stays empty.
    Second call: core fetch succeeds → channel resolved and cached."""
    route = respx.get("http://core.test/api/agents/sebas").mock(
        side_effect=[
            httpx.NetworkError("timeout"),
            httpx.Response(200, json={"id": "sebas", "name": "Sebas", "slack_channel_id": "CSEBAS"}),
        ]
    )
    a = SlackAdapter(_settings())
    a._client = AsyncMock()
    # name-lookup also finds nothing on the first call
    a._client.conversations_list.return_value = {
        "ok": True,
        "channels": [],
        "response_metadata": {"next_cursor": ""},
    }

    # First resolve — core raises, name lookup finds nothing → channel None
    ch1, username1, icon1, _v1 = await a._resolve("sebas")
    assert ch1 is None
    assert "sebas" not in a._cache  # cache must NOT contain the failed result

    # Second resolve — core succeeds → real channel returned and cached
    ch2, username2, icon2, _v2 = await a._resolve("sebas")
    assert ch2 == "CSEBAS"
    assert username2 == "Sebas"
    assert a._cache["sebas"] == ("CSEBAS", "Sebas", ":bust_in_silhouette:", "")
    assert route.call_count == 2  # both calls hit core (no cached poison)
    await a.stop()


@respx.mock
async def test_resolve_falls_back_to_conversations_list_by_name():
    respx.get("http://core.test/api/agents/maou").mock(
        return_value=httpx.Response(
            200, json={"id": "maou", "name": "Maou", "slack_channel_id": ""}
        )
    )
    a = SlackAdapter(_settings())
    a._client = AsyncMock()
    a._client.conversations_list.return_value = {
        "ok": True,
        "channels": [
            {"id": "CGENERAL", "name": "general"},
            {"id": "CMAOU", "name": "aegis-maou"},
        ],
        "response_metadata": {"next_cursor": ""},
    }
    channel, username, icon, _voice_id = await a._resolve("maou")
    assert channel == "CMAOU"
    assert username == "Maou"
    assert icon == ":moneybag:"
    await a.stop()


# --- send_voice (per-persona TTS voice notes) -------------------------------


async def test_send_voice_synthesizes_and_uploads(monkeypatch):
    a = _adapter(monkeypatch, voice_id="VOICE1")
    a._settings = _settings(AEGIS_ELEVENLABS_API_KEY="el-key")
    a._client.files_upload_v2.return_value = {"files": [{"ts": "5.5"}]}

    with respx.mock:
        route = respx.post("https://api.elevenlabs.io/v1/text-to-speech/VOICE1").mock(
            return_value=httpx.Response(200, content=b"\xff\xf3mp3bytes")
        )
        r = await a.send_voice(agent_id="pandoras-actor", text="all clear")

    assert route.called
    assert route.calls.last.request.headers["xi-api-key"] == "el-key"
    a._client.files_upload_v2.assert_awaited_once()
    ukw = a._client.files_upload_v2.await_args.kwargs
    assert ukw["channel"] == "C1"
    assert ukw["content"] == b"\xff\xf3mp3bytes"
    assert r.ok is True
    assert r.ref.to_dict() == {"adapter": "slack", "channel": "C1", "ts": "5.5"}


async def test_send_voice_no_voice_id_is_ok_false(monkeypatch):
    a = _adapter(monkeypatch, voice_id="")
    a._settings = _settings(AEGIS_ELEVENLABS_API_KEY="el-key")
    r = await a.send_voice(agent_id="sebas", text="hi")
    assert r.ok is False
    a._client.files_upload_v2.assert_not_awaited()


async def test_send_voice_no_api_key_is_ok_false(monkeypatch):
    a = _adapter(monkeypatch, voice_id="VOICE1")  # default _settings() has no key
    r = await a.send_voice(agent_id="sebas", text="hi")
    assert r.ok is False
    a._client.files_upload_v2.assert_not_awaited()
