"""Tests for agent CRUD endpoints."""

import base64

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


@pytest.fixture
def auth_headers():
    creds = base64.b64encode(b"admin:admin").decode()
    return {"Authorization": f"Basic {creds}"}


async def test_list_agents_requires_auth(app):
    """Agents endpoint requires authentication."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/agents")
        assert resp.status_code == 401


async def test_list_agents(app, auth_headers, mock_db_pool):
    """List agents returns agents from DB."""
    mock_db_pool.fetch.return_value = [
        {
            "id": "sebas",
            "name": "Sebas",
            "role": "executive-assistant",
            "description": "Butler",
            "capabilities": "[]",
            "active": True,
            "system_prompt": "You are Sebas",
            "avatar_url": None,
            "created_at": "2026-01-01T00:00:00Z",
        },
    ]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/agents", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["id"] == "sebas"


async def test_get_agent(app, auth_headers, mock_db_pool):
    """Get single agent by ID."""
    mock_db_pool.fetchrow.return_value = {
        "id": "sebas",
        "name": "Sebas",
        "role": "executive-assistant",
        "description": "Butler",
        "capabilities": "[]",
        "active": True,
        "system_prompt": "You are Sebas",
        "avatar_url": None,
        "created_at": "2026-01-01T00:00:00Z",
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/agents/sebas", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["id"] == "sebas"


async def test_get_agent_not_found(app, auth_headers, mock_db_pool):
    """Get non-existent agent returns 404."""
    mock_db_pool.fetchrow.return_value = None

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/agents/nonexistent", headers=auth_headers)
        assert resp.status_code == 404
