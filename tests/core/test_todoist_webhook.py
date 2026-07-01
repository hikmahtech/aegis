"""Tests for the Todoist webhook endpoint — HMAC verification + audit insert."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.config import Settings
from httpx import ASGITransport, AsyncClient

_TEST_REQUIRED_SETTINGS = {
    "database_url": "postgresql://test:test@localhost:5432/test",
    "litellm_url": "https://litellm.example.com/v1",
    "temporal_ui_url": "https://temporal.example.com",
    "n8n_ui_url": "https://n8n.example.com",
    "admin_username": "admin",
    "admin_password": "admin",
    "todoist_webhook_secret": "shh",
}


def _mock_pool():
    """In-memory pool that records inserted webhook events."""
    captured: list[tuple] = []
    conn = AsyncMock()

    async def _execute(query, *args):
        if "INSERT INTO todoist_webhook_events" in query:
            captured.append(args)
        return "INSERT 0 1"

    conn.execute = _execute

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    pool._captured = captured  # exposed for assertions
    return pool


@pytest.fixture
def settings():
    return Settings(**_TEST_REQUIRED_SETTINGS)


@pytest_asyncio.fixture(loop_scope="function")
async def client(settings):
    app = create_app(run_lifespan=False)
    app.state.db_pool = _mock_pool()
    app.dependency_overrides[get_settings] = lambda: settings
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.pool = app.state.db_pool  # expose for assertions
        yield c


def _sign(secret: str, body: bytes) -> str:
    # Todoist base64-encodes the digest (not hex, unlike GitHub/Sentry).
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


async def test_todoist_webhook_rejects_missing_signature(client):
    r = await client.post("/api/webhooks/todoist", content=b"{}")
    assert r.status_code == 401


async def test_todoist_webhook_rejects_bad_signature(client):
    r = await client.post(
        "/api/webhooks/todoist",
        content=b"{}",
        headers={"X-Todoist-Hmac-SHA256": "0" * 64},
    )
    assert r.status_code == 401


async def test_todoist_webhook_503_when_secret_missing(settings):
    """When AEGIS_TODOIST_WEBHOOK_SECRET is unset, return 503."""
    settings_no_secret = Settings(**{**_TEST_REQUIRED_SETTINGS, "todoist_webhook_secret": ""})
    app = create_app(run_lifespan=False)
    app.state.db_pool = _mock_pool()
    app.dependency_overrides[get_settings] = lambda: settings_no_secret
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(
            "/api/webhooks/todoist",
            content=b"{}",
            headers={"X-Todoist-Hmac-SHA256": "x"},
        )
    assert r.status_code == 503


async def test_todoist_webhook_inserts_audit_row(client):
    """Valid signature: handler returns 200 and inserts one audit row."""
    body = json.dumps({"event_name": "item:added", "event_data": {"id": 1}}).encode()
    r = await client.post(
        "/api/webhooks/todoist",
        content=body,
        headers={"X-Todoist-Hmac-SHA256": _sign("shh", body)},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"accepted": True}
    assert len(client.pool._captured) == 1
    event_name, event_data = client.pool._captured[0]
    assert event_name == "item:added"
    assert event_data == {"id": 1}
