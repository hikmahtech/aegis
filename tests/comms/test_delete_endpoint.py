"""Tests for the /api/comms/delete endpoint."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def fake_adapter():
    adapter = AsyncMock()
    adapter.name = "slack"
    adapter.delete_message = AsyncMock(return_value=True)
    return adapter


@pytest.fixture
def app(fake_adapter, monkeypatch):
    monkeypatch.setenv("AEGIS_API_KEY", "test-key")

    from aegis_comms.config import CommsSettings
    settings = CommsSettings(_env_file=None)

    from aegis_comms.__main__ import create_delivery_app
    return create_delivery_app(fake_adapter, settings)


async def test_delete_endpoint_calls_adapter_delete_message(app, fake_adapter):
    transport = ASGITransport(app=app)
    ref = {"adapter": "slack", "channel": "C100123", "ts": "4242.0"}
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/comms/delete",
            json={"delivery_ref": ref},
            headers={"X-API-Key": "test-key"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    fake_adapter.delete_message.assert_awaited_once()
    called_ref = fake_adapter.delete_message.await_args.kwargs["ref"]
    assert called_ref.adapter == "slack"
    assert called_ref.data["channel"] == "C100123"
    assert called_ref.data["ts"] == "4242.0"


async def test_delete_endpoint_returns_ok_false_on_adapter_exception(app, fake_adapter):
    fake_adapter.delete_message = AsyncMock(side_effect=Exception("channel gone"))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/comms/delete",
            json={"delivery_ref": {"adapter": "slack", "channel": "C1", "ts": "2.0"}},
            headers={"X-API-Key": "test-key"},
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


async def test_delete_endpoint_requires_auth(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/comms/delete",
            json={"delivery_ref": {"adapter": "slack", "channel": "C1", "ts": "2.0"}},
            headers={"X-API-Key": "wrong-key"},
        )
    assert resp.status_code == 401
