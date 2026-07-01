"""Test interactions list endpoint with comma-separated origin filter."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock

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
}


@pytest.fixture
def settings():
    return Settings(**_TEST_REQUIRED_SETTINGS)


@pytest.fixture
def auth_headers():
    creds = base64.b64encode(b"admin:admin").decode()
    return {"Authorization": f"Basic {creds}"}


@pytest_asyncio.fixture(loop_scope="function")
async def app_fixture(db_pool, settings):
    app = create_app(run_lifespan=False)
    app.state.db_pool = db_pool

    fake_client = AsyncMock()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_workflow_client] = lambda: fake_client

    # Ensure agent exists
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agents (id, name, role, system_prompt_path, telegram_topic_id, active) "
            "VALUES ('sebas', 'Sebas', 'assistant', 'personalities/sebas', 2753, TRUE) "
            "ON CONFLICT (id) DO NOTHING"
        )
        # Seed two interactions with distinct origins
        await conn.execute(
            "INSERT INTO interactions "
            "(flow_run_id, agent_id, kind, origin, prompt, status, timeout_policy) "
            "VALUES "
            "('run-a1', 'sebas', 'approval', 'alert_approve_investigate', 'q1', 'pending', 'archive'), "
            "('run-a2', 'sebas', 'approval', 'alert_approve_pr', 'q2', 'pending', 'archive'), "
            "('run-a3', 'sebas', 'approval', 'other_origin', 'q3', 'pending', 'archive')"
        )

    yield app

    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM interactions")


async def test_comma_separated_origin_filter(app_fixture, auth_headers):
    """origin=a,b returns rows with origin in {a, b} and excludes others."""
    transport = ASGITransport(app=app_fixture)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/interactions?origin=alert_approve_investigate,alert_approve_pr",
            headers=auth_headers,
        )
    assert resp.status_code == 200
    rows = resp.json()
    origins = {r["origin"] for r in rows}
    assert origins == {"alert_approve_investigate", "alert_approve_pr"}
    assert all(r["origin"] != "other_origin" for r in rows)


async def test_single_origin_still_works(app_fixture, auth_headers):
    """Single origin value (no comma) continues to work as before."""
    transport = ASGITransport(app=app_fixture)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/interactions?origin=other_origin",
            headers=auth_headers,
        )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["origin"] == "other_origin"


async def test_no_origin_filter_returns_all(app_fixture, auth_headers):
    """Omitting origin returns all interactions."""
    transport = ASGITransport(app=app_fixture)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/interactions",
            headers=auth_headers,
        )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) >= 3
