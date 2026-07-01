"""Tests for /api/agents/:id/tools route."""

from unittest.mock import AsyncMock

import pytest
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.config import Settings
from fastapi.testclient import TestClient


@pytest.fixture
def settings():
    return Settings(
        database_url="postgresql://test:test@localhost/test",
        litellm_url="https://litellm.test/v1",
        temporal_ui_url="https://temporal.test",
        n8n_ui_url="https://n8n.test",
        admin_username="admin",
        admin_password="admin",
        n8n_webhook_secret="test-secret",
        api_key="test-key",
    )


@pytest.fixture
def app(settings):
    app = create_app(run_lifespan=False)
    app.dependency_overrides[get_settings] = lambda: settings
    pool = AsyncMock()

    # The tools route now resolves the agent from the DB first (to prefer
    # metadata.tool_set), so stub fetchrow: known agent → a row with empty
    # metadata (falls back to AGENT_TOOL_SETS); unknown agent → None → 404.
    async def _fetchrow(_query, *args):
        agent_id = args[0] if args else None
        if agent_id == "sebas":
            return {"id": "sebas", "name": "Sebastian", "role": "chief", "metadata": {}}
        return None

    pool.fetchrow = _fetchrow
    app.state.db_pool = pool
    app.state.settings = settings
    return app


@pytest.fixture
def client(app):
    return TestClient(app, headers={"X-API-Key": "test-key"})


def test_agent_tools_requires_auth(app):
    unauthed = TestClient(app)
    assert unauthed.get("/api/agents/sebas/tools").status_code == 401


def test_agent_tools_known_agent(client):
    resp = client.get("/api/agents/sebas/tools")
    assert resp.status_code == 200
    tools = resp.json()
    assert isinstance(tools, list)
    assert len(tools) > 0
    assert all("name" in t and "description" in t for t in tools)
    names = {t["name"] for t in tools}
    assert "search_knowledge" in names
    assert "trigger_workflow" in names


def test_agent_tools_unknown_agent(client):
    resp = client.get("/api/agents/nonexistent/tools")
    assert resp.status_code == 404
