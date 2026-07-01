"""Tests for MCP tool dispatch endpoint.

The route is currently a stub: ``MCPManager.call_tool`` raises
``NotImplementedError`` and the endpoint translates that to HTTP 501. The
tests here assert the *real* contract — a happy-path test that mocks past
the stub would lie about production behaviour.
"""

from unittest.mock import AsyncMock

import pytest
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.config import Settings
from aegis.mcp_manager import MCPManager
from httpx import ASGITransport, AsyncClient


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
        mcp_servers={
            "docs-search": {"transport": "stdio", "command": ["python", "-m", "docs_search"]}
        },
    )


@pytest.fixture
def app(settings):
    app = create_app(run_lifespan=False)
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.settings = settings
    # Use the REAL MCPManager so call_tool raises NotImplementedError —
    # mocking past that would hide the fact that the endpoint isn't wired up.
    app.state.mcp_manager = MCPManager(settings.mcp_servers or {})
    app.state.db_pool = AsyncMock()
    return app


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_mcp_call_returns_501_until_client_implemented(client):
    """Until MCPManager.call_tool is implemented the endpoint must return 501.

    When the real MCP client lands this test should be replaced with a
    happy-path assertion against the live transport, NOT mocked back into
    a green check.
    """
    resp = await client.post(
        "/api/mcp/docs-search/search_docs",
        json={"query": "task flow"},
        headers={"X-API-Key": "test-key"},
    )

    assert resp.status_code == 501
    assert "not yet implemented" in resp.json()["detail"]


async def test_mcp_unknown_server(client, app):
    """MCP endpoint returns 404 for unknown server."""
    resp = await client.post(
        "/api/mcp/unknown/tool",
        json={},
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 404


async def test_mcp_no_auth(client):
    """MCP endpoint requires authentication."""
    resp = await client.post("/api/mcp/docs-search/tool", json={})
    assert resp.status_code in (401, 403)
