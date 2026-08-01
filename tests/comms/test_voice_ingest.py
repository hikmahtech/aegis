"""B3 — POST /api/ingest/voice (iOS Shortcut voice capture).

Comms stops at the core-client boundary here; the assertion that a
`kind="auto"` capture actually lands a row lives core-side in
tests/core/test_capture_auto_lane.py against a real Postgres.

Auth is fail-closed on purpose: an unset secret is a 503, so a deploy that
forgot the env var cannot silently accept unauthenticated audio.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

SECRET = "zzb3-voice-secret"


def _app(monkeypatch, *, secret: str = SECRET, el_key: str = "el-key"):
    monkeypatch.setenv("AEGIS_API_KEY", "test-key")
    monkeypatch.setenv("AEGIS_VOICE_INGEST_SECRET", secret)
    monkeypatch.setenv("AEGIS_ELEVENLABS_API_KEY", el_key)

    from aegis_comms.__main__ import create_delivery_app
    from aegis_comms.config import CommsSettings

    adapter = AsyncMock()
    adapter.name = "slack"
    return create_delivery_app(adapter, CommsSettings(_env_file=None))


@pytest.fixture
def core_capture(monkeypatch):
    """Patch SlackCoreClient.capture and record the calls."""
    calls: list[dict] = []

    async def _capture(self, *, text, external_id, kind="task"):
        calls.append({"text": text, "external_id": external_id, "kind": kind})
        return {"lane": "task", "task_ref": "T-77", "content_id": None}

    monkeypatch.setattr("aegis_comms.slack_inbound.SlackCoreClient.capture", _capture)
    return calls


@pytest.fixture
def fake_transcribe(monkeypatch):
    def _set(text: str | None):
        async def _transcribe(audio, **kwargs):
            return text

        monkeypatch.setattr("aegis_comms.elevenlabs.transcribe", _transcribe)

    return _set


async def _post(app, *, headers=None, content=b"fake-audio-bytes"):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(
            "/api/ingest/voice", content=content, headers=headers or {}
        )


async def test_missing_secret_config_is_503(monkeypatch, core_capture, fake_transcribe):
    fake_transcribe("should never be reached")
    app = _app(monkeypatch, secret="")
    resp = await _post(app, headers={"X-Voice-Secret": "anything"})
    assert resp.status_code == 503
    assert "not configured" in resp.json()["detail"]
    assert core_capture == []


async def test_wrong_secret_is_401(monkeypatch, core_capture, fake_transcribe):
    fake_transcribe("should never be reached")
    app = _app(monkeypatch)
    resp = await _post(app, headers={"X-Voice-Secret": "wrong"})
    assert resp.status_code == 401
    assert core_capture == []


async def test_absent_secret_header_is_401(monkeypatch, core_capture, fake_transcribe):
    fake_transcribe("should never be reached")
    app = _app(monkeypatch)
    resp = await _post(app)
    assert resp.status_code == 401
    assert core_capture == []


async def test_empty_body_is_400(monkeypatch, core_capture, fake_transcribe):
    fake_transcribe("should never be reached")
    app = _app(monkeypatch)
    resp = await _post(app, headers={"X-Voice-Secret": SECRET}, content=b"")
    assert resp.status_code == 400
    assert core_capture == []


async def test_valid_audio_captures_the_transcript(
    monkeypatch, core_capture, fake_transcribe
):
    fake_transcribe("remind me to renew the passport")
    app = _app(monkeypatch)
    resp = await _post(app, headers={"X-Voice-Secret": SECRET})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["transcript"] == "remind me to renew the passport"
    assert body["lane"] == "task"
    assert body["task_ref"] == "T-77"
    assert len(core_capture) == 1
    assert core_capture[0]["text"] == "remind me to renew the passport"
    assert core_capture[0]["kind"] == "auto"
    assert core_capture[0]["external_id"].startswith("voice:")


async def test_failed_transcription_is_502_not_an_empty_capture(
    monkeypatch, core_capture, fake_transcribe
):
    """A None transcript must not become a blank Inbox task."""
    fake_transcribe(None)
    app = _app(monkeypatch)
    resp = await _post(app, headers={"X-Voice-Secret": SECRET})
    assert resp.status_code == 502
    assert core_capture == []


async def test_no_elevenlabs_key_is_503(monkeypatch, core_capture):
    app = _app(monkeypatch, el_key="")
    resp = await _post(app, headers={"X-Voice-Secret": SECRET})
    assert resp.status_code == 503
    assert "transcription" in resp.json()["detail"]
    assert core_capture == []
