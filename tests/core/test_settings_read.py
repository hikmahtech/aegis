"""Tests for GET /api/settings/{key} endpoint."""

import base64
from unittest.mock import AsyncMock

import pytest
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def app(test_settings, mock_db_pool):
    application = create_app(run_lifespan=False)
    application.dependency_overrides[get_settings] = lambda: test_settings
    application.state.db_pool = mock_db_pool
    return application


def _auth_headers() -> dict:
    creds = base64.b64encode(b"admin:admin").decode()
    return {"Authorization": f"Basic {creds}"}


AUTH_HEADERS = _auth_headers()


async def test_get_setting_exists(app, mock_db_pool):
    """Returns key and value when setting exists."""
    topics = {"topics": [{"name": "ai", "queries": ["LLM"], "priority": "high"}]}
    mock_db_pool.fetchrow = AsyncMock(return_value={"value": topics})

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/settings/intelligence_topics", headers=AUTH_HEADERS)

    assert resp.status_code == 200
    data = resp.json()
    assert data["key"] == "intelligence_topics"
    assert data["value"]["topics"][0]["name"] == "ai"


async def test_get_setting_not_found(app, mock_db_pool):
    """Returns key with null value when setting does not exist."""
    mock_db_pool.fetchrow = AsyncMock(return_value=None)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/settings/nonexistent", headers=AUTH_HEADERS)

    assert resp.status_code == 200
    data = resp.json()
    assert data["key"] == "nonexistent"
    assert data["value"] is None


async def test_get_setting_requires_auth(app):
    """Settings endpoint requires authentication."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/settings/some_key")

    assert resp.status_code == 401


async def test_get_setting_string_value(app, mock_db_pool):
    """Returns a simple string value correctly."""
    mock_db_pool.fetchrow = AsyncMock(return_value={"value": "enabled"})

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/settings/feature_flag", headers=AUTH_HEADERS)

    assert resp.status_code == 200
    data = resp.json()
    assert data["key"] == "feature_flag"
    assert data["value"] == "enabled"


async def test_get_setting_boolean_value(app, mock_db_pool):
    """Returns a boolean value correctly."""
    mock_db_pool.fetchrow = AsyncMock(return_value={"value": True})

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/settings/tool_calling_enabled", headers=AUTH_HEADERS)

    assert resp.status_code == 200
    data = resp.json()
    assert data["key"] == "tool_calling_enabled"
    assert data["value"] is True
