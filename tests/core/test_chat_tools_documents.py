"""Tests for the document-attachment chat tools (youtube_transcript / pdf_to_text).

The executors fetch content via aegis.services.content_extract and deliver it
to the comms /api/deliver/document endpoint as a .txt attachment targeted at
the channel the user's message came from (chat_context.delivery_ref). The LLM
only ever sees a short JSON confirmation, never the full text.
"""

from __future__ import annotations

import json

import httpx
import respx
from aegis.services.chat import ToolContext, _exec_pdf_to_text, _exec_youtube_transcript


def _ctx(comms_url="http://comms:8081", channel="C123"):
    settings = type("S", (), {"comms_url": comms_url, "api_key": "test-key"})()
    chat_context = {"user_message": "x", "thread_id": "t"}
    if channel:
        chat_context["delivery_ref"] = {"adapter": "slack", "channel": channel}
    return ToolContext(agent_id="sebas", settings=settings, chat_context=chat_context)


async def _fake_transcript(url):
    return "hello world " * 100, {"video_id": "dQw4w9WgXcQ", "segments": 42}


async def test_youtube_transcript_delivers_attachment(monkeypatch):
    from aegis.services import content_extract

    monkeypatch.setattr(content_extract, "fetch_youtube_transcript", _fake_transcript)
    with respx.mock:
        deliver = respx.post("http://comms:8081/api/deliver/document").mock(
            return_value=httpx.Response(200, json={"ok": True, "count": 1})
        )
        out = json.loads(
            await _exec_youtube_transcript(
                None, {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}, _ctx()
            )
        )
    assert out["ok"] is True
    assert out["video_id"] == "dQw4w9WgXcQ"
    body = json.loads(deliver.calls.last.request.content)
    assert body["target"] == {"channel": "C123"}
    assert body["agent_id"] == "sebas"
    assert body["documents"][0]["filename"] == "youtube-dQw4w9WgXcQ-transcript.txt"
    assert "hello world" in body["documents"][0]["content"]
    assert deliver.calls.last.request.headers["x-api-key"] == "test-key"
    # The LLM-facing result must not embed the full transcript.
    assert len(out.get("preview", "")) <= 300


async def test_youtube_transcript_rejects_non_youtube_url():
    out = json.loads(await _exec_youtube_transcript(None, {"url": "https://example.com"}, _ctx()))
    assert "error" in out


async def test_youtube_transcript_no_captions(monkeypatch):
    from aegis.services import content_extract

    async def _none(url):
        return "", {"video_id": "dQw4w9WgXcQ"}

    monkeypatch.setattr(content_extract, "fetch_youtube_transcript", _none)
    out = json.loads(
        await _exec_youtube_transcript(None, {"url": "https://youtu.be/dQw4w9WgXcQ"}, _ctx())
    )
    assert "No transcript" in out["error"]


async def test_youtube_transcript_delivery_failure_reported(monkeypatch):
    from aegis.services import content_extract

    monkeypatch.setattr(content_extract, "fetch_youtube_transcript", _fake_transcript)
    with respx.mock:
        respx.post("http://comms:8081/api/deliver/document").mock(
            return_value=httpx.Response(500)
        )
        out = json.loads(
            await _exec_youtube_transcript(None, {"url": "https://youtu.be/dQw4w9WgXcQ"}, _ctx())
        )
    assert "delivery failed" in out["error"]


async def test_youtube_transcript_without_comms_url(monkeypatch):
    from aegis.services import content_extract

    monkeypatch.setattr(content_extract, "fetch_youtube_transcript", _fake_transcript)
    out = json.loads(
        await _exec_youtube_transcript(
            None, {"url": "https://youtu.be/dQw4w9WgXcQ"}, _ctx(comms_url="")
        )
    )
    assert "comms_url" in out["error"]


async def test_pdf_to_text_delivers_attachment(monkeypatch):
    from aegis.services import content_extract

    async def _fake_extract(url, content_type=None, max_chars=100_000):
        return "PDF BODY TEXT", None

    monkeypatch.setattr(content_extract, "fetch_and_extract", _fake_extract)
    with respx.mock:
        deliver = respx.post("http://comms:8081/api/deliver/document").mock(
            return_value=httpx.Response(200, json={"ok": True, "count": 1})
        )
        out = json.loads(
            await _exec_pdf_to_text(None, {"url": "https://x.test/paper.pdf"}, _ctx())
        )
    assert out["ok"] is True
    body = json.loads(deliver.calls.last.request.content)
    assert body["documents"][0]["filename"] == "paper.txt"
    assert body["documents"][0]["content"] == "PDF BODY TEXT"
    assert body["target"] == {"channel": "C123"}


async def test_pdf_to_text_requires_http_url():
    out = json.loads(await _exec_pdf_to_text(None, {"url": "paper.pdf"}, _ctx()))
    assert "error" in out


async def test_pdf_to_text_extraction_failure(monkeypatch):
    from aegis.services import content_extract

    async def _empty(url, content_type=None, max_chars=100_000):
        return "", None

    monkeypatch.setattr(content_extract, "fetch_and_extract", _empty)
    out = json.loads(await _exec_pdf_to_text(None, {"url": "https://x.test/a.pdf"}, _ctx()))
    assert "Could not extract" in out["error"]


async def test_delivery_falls_back_to_agent_channel_without_delivery_ref(monkeypatch):
    from aegis.services import content_extract

    monkeypatch.setattr(content_extract, "fetch_youtube_transcript", _fake_transcript)
    with respx.mock:
        deliver = respx.post("http://comms:8081/api/deliver/document").mock(
            return_value=httpx.Response(200, json={"ok": True, "count": 1})
        )
        out = json.loads(
            await _exec_youtube_transcript(
                None, {"url": "https://youtu.be/dQw4w9WgXcQ"}, _ctx(channel=None)
            )
        )
    assert out["ok"] is True
    body = json.loads(deliver.calls.last.request.content)
    assert body["target"] is None
