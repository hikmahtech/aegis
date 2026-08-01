"""B3 — a spoken "remember …" files instead of chatting.

The behaviour under test is a FORK: a capture-intent transcript must reach
`SlackCoreClient.capture(kind="auto")` and NOT `_route_and_dispatch`, and a
normal transcript must still reach `_route_and_dispatch` and NOT `capture`.
Both directions are asserted in both tests, because a one-sided assertion
("capture was called") passes just as happily when the branch fires for
everything.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aegis_comms.slack_inbound import SlackInbound, capture_ack, capture_intent_text

CHANNEL = "CSEBAS"


def _inbound(transcript: str):
    """SlackInbound wired so `_handle_audio_file` reaches the transcript fork."""
    core = AsyncMock()
    core.capture.return_value = {
        "lane": "life_fact",
        "content_id": "abcdef0123456789",
        "task_ref": None,
    }
    adapter = AsyncMock()
    inbound = SlackInbound(
        adapter=adapter,
        core=core,
        channel_agent_map={CHANNEL: "sebas"},
        bot_user_id="UBOT",
        elevenlabs_api_key="el-key",
    )
    inbound._download_private_file = AsyncMock(return_value=b"audio-bytes")
    inbound._route_and_dispatch = AsyncMock()
    return inbound, core, adapter


@pytest.fixture
def fake_transcribe(monkeypatch):
    def _set(text: str):
        async def _transcribe(audio, **kwargs):
            return text

        monkeypatch.setattr("aegis_comms.elevenlabs.transcribe", _transcribe)

    return _set


async def test_remember_transcript_is_captured_not_chatted(fake_transcribe):
    fake_transcribe("Remember, my passport expires in March 2030.")
    inbound, core, adapter = _inbound("")

    await inbound._handle_audio_file(
        name="voice.m4a", url="https://files/x", channel_id=CHANNEL, caption=""
    )

    core.capture.assert_awaited_once()
    kwargs = core.capture.await_args.kwargs
    assert kwargs["kind"] == "auto"
    # The opener is stripped — the classifier must see the fact, not the verb.
    assert kwargs["text"] == "my passport expires in March 2030."
    inbound._route_and_dispatch.assert_not_awaited()
    # The resolved lane is echoed back so the human sees where it went.
    posted = [c.kwargs["text"] for c in adapter.send_message.await_args_list]
    assert any("life fact" in t for t in posted), posted


async def test_normal_transcript_still_routes_to_chat(fake_transcribe):
    fake_transcribe("What time does the tip close on Sundays?")
    inbound, core, _adapter = _inbound("")

    await inbound._handle_audio_file(
        name="voice.m4a", url="https://files/x", channel_id=CHANNEL, caption=""
    )

    inbound._route_and_dispatch.assert_awaited_once()
    assert (
        inbound._route_and_dispatch.await_args.kwargs["text"]
        == "What time does the tip close on Sundays?"
    )
    core.capture.assert_not_awaited()


async def test_remembering_is_not_a_capture_opener(fake_transcribe):
    """Word-boundary guard: "remembering ..." is prose, not an instruction."""
    fake_transcribe("Remembering to buy milk is the hard part.")
    inbound, core, _adapter = _inbound("")

    await inbound._handle_audio_file(
        name="voice.m4a", url="https://files/x", channel_id=CHANNEL, caption=""
    )

    core.capture.assert_not_awaited()
    inbound._route_and_dispatch.assert_awaited_once()


@pytest.mark.parametrize(
    ("transcript", "expected"),
    [
        ("remember the bins go out Tuesday", "the bins go out Tuesday"),
        ("Note to self: cancel the gym", "cancel the gym"),
        ("Capture — call the plumber", "call the plumber"),
        ("make a note, boiler serviced in June", "boiler serviced in June"),
        ("add to inbox: renew the passport", "renew the passport"),
        ("remember", None),  # bare opener carries no content
        ("rememberance day is in November", None),
        ("what's the weather", None),
    ],
)
def test_capture_intent_text(transcript, expected):
    assert capture_intent_text(transcript) == expected


def test_capture_ack_names_the_lane():
    assert "life fact" in capture_ack({"lane": "life_fact", "content_id": "cid12345678"})
    assert "Inbox" in capture_ack({"lane": "task", "task_ref": "T-1"})
    assert "failed" in capture_ack(None)
    # Lane says life_fact but nothing was written — must not claim success.
    assert "failed" in capture_ack({"lane": "life_fact", "content_id": None})
