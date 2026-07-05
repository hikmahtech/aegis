"""GitHub webhook HMAC verification.

Spec §11 Phase 2 validation: 'signed GitHub test delivery returns 200;
unsigned returns 401.'
"""

from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.api.routes.interactions import get_workflow_client
from aegis.config import Settings
from httpx import ASGITransport, AsyncClient

_TEST_REQUIRED_SETTINGS = {
    "database_url": "postgresql://test:test@localhost:5432/test",
    "litellm_url": "https://litellm.example.com/v1",
    "temporal_ui_url": "https://temporal.example.com",
    "n8n_ui_url": "https://n8n.example.com",
    "admin_username": "admin",
    "admin_password": "admin",
    "n8n_webhook_secret": "test-secret",
    "github_webhook_secret": "test-secret-abc",
}


def _mock_temporal_stub():
    handle = MagicMock()
    handle.id = "wf-test"
    client = MagicMock()
    client.start_workflow = AsyncMock(return_value=handle)
    return client


def _mock_pool():
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value="del-new")

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    # resolve_tag("infra") → pandoras-actor (seed mapping).
    pool.fetch = AsyncMock(return_value=[{"id": "pandoras-actor"}])
    return pool


@pytest.fixture
def settings():
    return Settings(**_TEST_REQUIRED_SETTINGS)


@pytest_asyncio.fixture(loop_scope="function")
async def client(settings):
    app = create_app(run_lifespan=False)
    app.state.db_pool = _mock_pool()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_workflow_client] = lambda: _mock_temporal_stub()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def test_signed_github_delivery_returns_200(client):
    body = json.dumps({"action": "opened", "number": 42}).encode()
    sig = _sign("test-secret-abc", body)
    resp = await client.post(
        "/api/webhooks/github",
        content=body,
        headers={
            "X-Hub-Signature-256": sig,
            "X-GitHub-Event": "pull_request",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 200, resp.text
    body_json = resp.json()
    assert body_json["accepted"] is True
    assert body_json["event"] == "pull_request"


async def test_unsigned_github_delivery_returns_401(client):
    resp = await client.post(
        "/api/webhooks/github",
        content=b'{"action": "opened"}',
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 401


async def test_bad_signature_returns_401(client):
    resp = await client.post(
        "/api/webhooks/github",
        content=b'{"action": "opened"}',
        headers={
            "X-Hub-Signature-256": "sha256=" + "00" * 32,
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 401


async def test_missing_secret_setting_returns_503():
    """When the secret is empty, the endpoint reports misconfiguration, not pretend-accept."""
    settings_no_secret = Settings(**{**_TEST_REQUIRED_SETTINGS, "github_webhook_secret": ""})
    app = create_app(run_lifespan=False)
    app.state.db_pool = _mock_pool()
    app.dependency_overrides[get_settings] = lambda: settings_no_secret
    app.dependency_overrides[get_workflow_client] = lambda: _mock_temporal_stub()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/api/webhooks/github",
            content=b"{}",
            headers={"X-Hub-Signature-256": "sha256=deadbeef"},
        )
    assert resp.status_code == 503
