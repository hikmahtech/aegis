"""Tests for the MCP introspection + tool-dispatch endpoints.

The route talks to a real :class:`~aegis.mcp_manager.MCPManager` over a real
``httpx`` stack; only the remote MCP server is faked (``respx``). Mocking the
manager itself would prove nothing about production.

Transport-level failure isolation lives in ``test_mcp_manager.py``; this file
pins the HTTP contract — status codes and bodies.
"""

import json
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.config import Settings
from aegis.mcp_manager import MCPManager
from httpx import ASGITransport, AsyncClient

MCP_URL = "https://zzb8-docs.invalid/mcp"

_INIT_RESULT = {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "serverInfo": {"name": "zzb8-docs", "version": "1"},
}
_TOOLS = [{"name": "search_docs", "description": "Search the docs", "inputSchema": {}}]


def _fake_mcp_server(call_result=None):
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        method = payload.get("method")
        if method == "initialize":
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": payload["id"], "result": _INIT_RESULT}
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": payload["id"], "result": {"tools": _TOOLS}}
            )
        result = (
            call_result
            if call_result is not None
            else {"content": [{"type": "text", "text": "ok"}]}
        )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    return handler


def _settings(*, enabled: bool = True, servers=None) -> Settings:
    if servers is None:
        servers = {"docs-search": {"transport": "streamable-http", "url": MCP_URL}}
    return Settings(
        database_url="postgresql://test:test@localhost/test",
        litellm_url="https://litellm.test/v1",
        temporal_ui_url="https://temporal.test",
        n8n_ui_url="https://n8n.test",
        admin_username="admin",
        admin_password="admin",
        n8n_webhook_secret="test-secret",
        api_key="test-key",
        mcp_enabled=enabled,
        mcp_servers=servers,
    )


def _app(settings: Settings):
    app = create_app(run_lifespan=False)
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.settings = settings
    # The REAL manager, wired exactly as api/app.py wires it.
    app.state.mcp_manager = MCPManager(
        server_configs=settings.mcp_servers or {}, enabled=settings.mcp_enabled
    )
    app.state.db_pool = AsyncMock()
    return app


@pytest.fixture
def settings():
    return _settings()


@pytest.fixture
def app(settings):
    return _app(settings)


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await app.state.mcp_manager.close()


AUTH = {"X-API-Key": "test-key"}


# -- preserved contract ----------------------------------------------------


async def test_mcp_unknown_server(client, app):
    """MCP endpoint returns 404 for unknown server."""
    resp = await client.post(
        "/api/mcp/unknown/tool",
        json={},
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 404


async def test_mcp_unknown_server_body_names_the_server(client):
    """The 404 is enforced twice over (route pre-check + manager); pin the body."""
    resp = await client.post("/api/mcp/zzb8-nope/tool", json={}, headers=AUTH)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "MCP server 'zzb8-nope' not configured"


async def test_mcp_no_auth(client):
    """MCP endpoint requires authentication."""
    resp = await client.post("/api/mcp/docs-search/tool", json={})
    assert resp.status_code in (401, 403)


async def test_mcp_no_auth_on_read_routes(client):
    for path in ("/api/mcp", "/api/mcp/docs-search/tools"):
        resp = await client.get(path)
        assert resp.status_code in (401, 403), path


# -- live transport --------------------------------------------------------


async def test_call_tool_executes_against_the_configured_server(client):
    """Replaces the old 501 assertion: the call now reaches a real transport."""
    async with respx.mock(assert_all_called=False) as router:
        router.post(MCP_URL).mock(
            side_effect=_fake_mcp_server(
                call_result={"content": [{"type": "text", "text": "task flow docs"}]}
            )
        )
        resp = await client.post(
            "/api/mcp/docs-search/search_docs", json={"query": "task flow"}, headers=AUTH
        )
        # The arguments really went to the server as MCP tool arguments.
        sent = [json.loads(c.request.content) for c in router.calls]

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "ok": True,
        "result": {"content": [{"type": "text", "text": "task flow docs"}]},
    }
    call = next(m for m in sent if m.get("method") == "tools/call")
    assert call["params"] == {"name": "search_docs", "arguments": {"query": "task flow"}}


async def test_list_tools_returns_the_servers_tools(client):
    async with respx.mock(assert_all_called=False) as router:
        router.post(MCP_URL).mock(side_effect=_fake_mcp_server())
        resp = await client.get("/api/mcp/docs-search/tools", headers=AUTH)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["server"] == "docs-search"
    assert [t["name"] for t in body["tools"]] == ["search_docs"]
    # Discovery must not execute anything.
    methods = [json.loads(c.request.content).get("method") for c in router.calls]
    assert "tools/call" not in methods


async def test_list_servers_reports_config_without_the_token():
    settings = _settings(
        servers={
            "docs-search": {"url": MCP_URL, "auth_token": "zzb8-endpoint-token"},
            "zzb8-broken": {"transport": "stdio", "command": "/bin/sh"},
        }
    )
    app = _app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/api/mcp", headers=AUTH)
    await app.state.mcp_manager.close()

    assert resp.status_code == 200, resp.text
    rows = {row["name"]: row for row in resp.json()["servers"]}
    assert rows["docs-search"]["usable"] is True
    assert rows["docs-search"]["auth"] is True
    assert rows["zzb8-broken"]["usable"] is False
    assert "stdio" in rows["zzb8-broken"]["error"]
    assert "zzb8-endpoint-token" not in resp.text


async def test_unreachable_server_is_a_bounded_500_that_does_not_wedge_others():
    settings = _settings(
        servers={
            "dead": {"url": "https://zzb8-dead.invalid/mcp"},
            "docs-search": {"url": MCP_URL},
        }
    )
    app = _app(settings)
    transport = ASGITransport(app=app)
    async with respx.mock(assert_all_called=False) as router:
        router.post("https://zzb8-dead.invalid/mcp").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        router.post(MCP_URL).mock(side_effect=_fake_mcp_server())
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            dead = await c.post("/api/mcp/dead/anything", json={}, headers=AUTH)
            healthy = await c.post("/api/mcp/docs-search/search_docs", json={}, headers=AUTH)
    await app.state.mcp_manager.close()

    assert dead.status_code == 500
    detail = dead.json()["detail"]
    assert "dead" in detail and len(detail) < 300
    # A dead server must not poison the healthy one.
    assert healthy.status_code == 200, healthy.text
    assert healthy.json()["ok"] is True


async def test_timeout_is_reported_as_504():
    settings = _settings(servers={"docs-search": {"url": MCP_URL, "timeout_s": 0.2}})
    app = _app(settings)
    transport = ASGITransport(app=app)

    async def never_answers(request):
        import asyncio

        await asyncio.sleep(30)
        return httpx.Response(200, json={})

    async with respx.mock(assert_all_called=False) as router:
        router.post(MCP_URL).mock(side_effect=never_answers)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/api/mcp/docs-search/x", json={}, headers=AUTH)
    await app.state.mcp_manager.close()

    assert resp.status_code == 504
    assert "budget" in resp.json()["detail"]


async def test_malformed_server_response_is_a_500_not_a_null_result():
    async with respx.mock(assert_all_called=False) as router:
        router.post(MCP_URL).mock(return_value=httpx.Response(200, content=b"<html>hi</html>"))
        app = _app(_settings())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/api/mcp/docs-search/x", json={}, headers=AUTH)
        await app.state.mcp_manager.close()

    assert resp.status_code == 500
    assert "docs-search" in resp.json()["detail"]


async def test_non_object_body_is_rejected():
    app = _app(_settings())
    transport = ASGITransport(app=app)
    async with respx.mock(assert_all_called=False) as router:
        route = router.post(MCP_URL).mock(side_effect=_fake_mcp_server())
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/api/mcp/docs-search/x", json=["a", "b"], headers=AUTH)
    await app.state.mcp_manager.close()

    assert resp.status_code == 422
    assert route.call_count == 0


# -- feature flag ----------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/mcp/docs-search/search_docs"),
        ("GET", "/api/mcp/docs-search/tools"),
        ("GET", "/api/mcp"),
    ],
)
async def test_disabled_flag_is_503_on_every_route_and_contacts_nobody(method, path):
    app = _app(_settings(enabled=False))
    transport = ASGITransport(app=app)
    async with respx.mock(assert_all_called=False) as router:
        route = router.post(MCP_URL).mock(side_effect=_fake_mcp_server())
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            if method == "POST":
                resp = await c.post(path, json={}, headers=AUTH)
            else:
                resp = await c.get(path, headers=AUTH)
    await app.state.mcp_manager.close()

    assert resp.status_code == 503, resp.text
    assert "disabled" in resp.json()["detail"]
    assert route.call_count == 0
