"""The neutral /api/deliver/card endpoint hands a CardSpec to the active adapter."""

from unittest.mock import AsyncMock

import pytest
from aegis_comms.adapters.base import CardSpec, DeliveryRef, SendResult
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def fake_adapter():
    adapter = AsyncMock()
    adapter.name = "telegram"
    adapter.send_card = AsyncMock(
        return_value=SendResult(
            ok=True,
            ref=DeliveryRef("telegram", {"chat_id": 12345, "message_id": 99, "topic_id": 2755}),
        )
    )
    return adapter


@pytest.fixture
def app(fake_adapter, monkeypatch):
    monkeypatch.setenv("AEGIS_TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setenv("AEGIS_API_KEY", "test-key")
    monkeypatch.setenv("AEGIS_TELEGRAM_BOT_TOKEN", "test:token")

    from aegis_comms.config import TelegramSettings

    settings = TelegramSettings(_env_file=None)

    from aegis_comms.__main__ import create_delivery_app

    return create_delivery_app(fake_adapter, settings)


async def test_card_endpoint_builds_cardspec(app, fake_adapter, monkeypatch):
    import aegis_comms.__main__ as bot_main

    monkeypatch.setattr(bot_main, "_log_dispatch", AsyncMock(return_value=None))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/deliver/card",
            json={
                "interaction_id": "ia-1",
                "agent_id": "sebas",
                "kind": "approval",
                "prompt": "Reply to proceed",
                "options": None,
            },
            headers={"X-API-Key": "test-key"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    # back-compat mirror still surfaces the message id for the worker
    assert body["message_id"] == 99

    fake_adapter.send_card.assert_awaited_once()
    spec = fake_adapter.send_card.await_args.args[0]
    assert isinstance(spec, CardSpec)
    assert spec.interaction_id == "ia-1"
    assert spec.agent_id == "sebas"
    assert spec.kind == "approval"
    assert spec.prompt == "Reply to proceed"
    assert spec.options is None
    assert spec.target is None


async def test_card_endpoint_requires_auth(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/deliver/card",
            json={
                "interaction_id": "ia-1",
                "agent_id": "sebas",
                "kind": "approval",
                "prompt": "p",
                "options": None,
            },
            headers={"X-API-Key": "wrong-key"},
        )
    assert resp.status_code == 401
