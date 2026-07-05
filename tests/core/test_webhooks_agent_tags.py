"""Issue #36 — webhook workflow starts resolve their agent by behavior tag
(GitHub/Sentry → `infra`, Todoist clarify → `gtd`) instead of a literal id.

When no active agent holds the tag, the webhook must still return 2xx to the
external caller (Sentry/GitHub) and simply NOT start the workflow.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
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
    "github_webhook_secret": "test-secret-abc",
    "sentry_webhook_secret": "test-secret-abc",
}


def _mock_temporal_stub():
    handle = MagicMock()
    handle.id = "wf-test"
    client = MagicMock()
    client.start_workflow = AsyncMock(return_value=handle)
    return client


def _mock_pool(agent_rows):
    """Pool whose fetch() (used by resolve_tag) returns `agent_rows`, and whose
    acquire()/fetchval claims a fresh (non-duplicate) idempotency row."""
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value="del-new")

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    pool.fetch = AsyncMock(return_value=agent_rows)
    return pool


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(b"test-secret-abc", body, hashlib.sha256).hexdigest()


def _sentry_sign(body: bytes) -> str:
    return hmac.new(b"test-secret-abc", body, hashlib.sha256).hexdigest()


@pytest.fixture
def settings():
    return Settings(**_TEST_REQUIRED_SETTINGS)


async def _make_client(settings, pool, temporal):
    app = create_app(run_lifespan=False)
    app.state.db_pool = pool
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_workflow_client] = lambda: temporal
    return app


@pytest.mark.asyncio
async def test_github_webhook_resolves_infra_agent(settings):
    """A renamed infra agent (not 'pandoras-actor') is passed to the flow."""
    pool = _mock_pool([{"id": "custom-ops"}])
    temporal = _mock_temporal_stub()
    app = await _make_client(settings, pool, temporal)
    body = json.dumps({"action": "opened"}).encode()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/api/webhooks/github",
            content=body,
            headers={"X-Hub-Signature-256": _sign(body), "X-GitHub-Event": "pull_request"},
        )
    assert resp.status_code == 200
    assert temporal.start_workflow.await_count == 1
    assert temporal.start_workflow.call_args.args[1]["agent_id"] == "custom-ops"


@pytest.mark.asyncio
async def test_github_webhook_no_infra_agent_skips_but_200(settings):
    """No agent holds `infra` → 2xx to GitHub, workflow NOT started."""
    pool = _mock_pool([])  # resolve_tag → None
    temporal = _mock_temporal_stub()
    app = await _make_client(settings, pool, temporal)
    body = json.dumps({"action": "opened"}).encode()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/api/webhooks/github",
            content=body,
            headers={"X-Hub-Signature-256": _sign(body), "X-GitHub-Event": "push"},
        )
    assert resp.status_code == 200
    assert resp.json()["skipped"] == "no_infra_agent"
    assert temporal.start_workflow.await_count == 0


@pytest.mark.asyncio
async def test_sentry_webhook_no_infra_agent_skips_but_200(settings):
    pool = _mock_pool([])
    temporal = _mock_temporal_stub()
    app = await _make_client(settings, pool, temporal)
    body = json.dumps({"data": {"issue": {"id": "issue-99", "title": "Crash"}}}).encode()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/api/webhooks/sentry",
            content=body,
            headers={"Sentry-Hook-Signature": _sentry_sign(body)},
        )
    assert resp.status_code == 200
    assert resp.json()["skipped"] == "no_infra_agent"
    assert temporal.start_workflow.await_count == 0
