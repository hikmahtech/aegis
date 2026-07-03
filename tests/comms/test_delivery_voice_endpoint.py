"""The /api/deliver/voice endpoint hands (agent_id, text) to adapter.send_voice."""

from unittest.mock import AsyncMock

import pytest
from aegis_comms.adapters.base import DeliveryRef, SendResult
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def fake_adapter():
    adapter = AsyncMock()
    adapter.name = "slack"
    adapter.send_voice = AsyncMock(
        return_value=SendResult(ok=True, ref=DeliveryRef("slack", {"channel": "C1", "ts": "9.9"}))
    )
    return adapter


@pytest.fixture
def app(fake_adapter, monkeypatch):
    monkeypatch.setenv("AEGIS_API_KEY", "test-key")
    monkeypatch.setenv("AEGIS_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("AEGIS_SLACK_APP_TOKEN", "xapp-test")

    from aegis_comms.config import CommsSettings

    settings = CommsSettings(_env_file=None)

    from aegis_comms.__main__ import create_delivery_app

    return create_delivery_app(fake_adapter, settings)


async def test_voice_endpoint_calls_send_voice(app, fake_adapter):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/deliver/voice",
            json={"agent_id": "pandoras-actor", "text": "investigation complete"},
            headers={"X-API-Key": "test-key"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["agent_id"] == "pandoras-actor"
    fake_adapter.send_voice.assert_awaited_once()
    kw = fake_adapter.send_voice.await_args.kwargs
    assert kw["agent_id"] == "pandoras-actor"
    assert kw["text"] == "investigation complete"


async def test_voice_endpoint_reports_not_configured_as_ok_false(app, fake_adapter):
    """A skipped voice note (no voice_id / no key) surfaces ok=False, not a 500."""
    fake_adapter.send_voice = AsyncMock(
        return_value=SendResult(ok=False, used_html=False, error="tts_not_configured")
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/deliver/voice",
            json={"agent_id": "sebas", "text": "hello"},
            headers={"X-API-Key": "test-key"},
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


async def test_voice_endpoint_requires_api_key(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/deliver/voice",
            json={"agent_id": "sebas", "text": "hello"},
            headers={"X-API-Key": "wrong"},
        )
    assert resp.status_code == 401
