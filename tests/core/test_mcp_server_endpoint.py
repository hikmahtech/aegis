"""POST /api/mcp-server/{agent_id} — AEGIS's chat tools served as an MCP server.

Real app, real Postgres pool, real agent row: the whole point of the endpoint is
that the tool list comes from the agent's DB `metadata.tool_set`, so a stubbed
pool would test nothing. Only the tool *executor* is swapped out (one dict entry
in `aegis.services.chat.TOOL_EXECUTORS`), because running the real one would
need Todoist.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
import structlog.testing
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.config import Settings
from aegis.services import chat as chat_mod
from httpx import ASGITransport, AsyncClient

AGENT = "zzb9-mcp-agent"
UNKNOWN_AGENT = "zzb9-nobody"
PATH = f"/api/mcp-server/{AGENT}"
AUTH = {"X-API-Key": "test-key"}

# `call_mcp_tool` is granted in the DB on purpose: the endpoint must strip it.
TOOL_SET = ["whats_next", "capture_to_inbox", "call_mcp_tool"]


def _settings(*, enabled: bool = True) -> Settings:
    return Settings(
        database_url="postgresql://test:test@localhost/test",
        litellm_url="https://litellm.test/v1",
        temporal_ui_url="https://temporal.test",
        n8n_ui_url="https://n8n.test",
        admin_username="admin",
        admin_password="admin",
        n8n_webhook_secret="test-secret",
        api_key="test-key",
        mcp_server_enabled=enabled,
    )


def _app(settings: Settings, db_pool):
    app = create_app(run_lifespan=False)
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.settings = settings
    app.state.db_pool = db_pool
    return app


@pytest_asyncio.fixture(loop_scope="function")
async def agent_row(db_pool):
    """A real agent whose tool_set is exactly TOOL_SET."""
    await db_pool.execute("DELETE FROM agents WHERE id = $1", AGENT)
    await db_pool.execute(
        "INSERT INTO agents (id, name, role, system_prompt_path, metadata, active) "
        "VALUES ($1, 'Zzb9', 'test', '', $2, true)",
        AGENT,
        {"tool_set": TOOL_SET},
    )
    yield AGENT
    await db_pool.execute("DELETE FROM agents WHERE id = $1", AGENT)


@pytest_asyncio.fixture(loop_scope="function")
async def client(db_pool, agent_row):
    app = _app(_settings(), db_pool)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _rpc(client, method: str, *, params=None, rpc_id=1, path: str = PATH, headers=AUTH):
    body: dict = {"jsonrpc": "2.0", "method": method}
    if rpc_id is not None:
        body["id"] = rpc_id
    if params is not None:
        body["params"] = params
    return await client.post(path, json=body, headers=headers)


# -- gates -----------------------------------------------------------------


async def test_disabled_setting_refuses_and_says_how_to_enable(db_pool, agent_row):
    app = _app(_settings(enabled=False), db_pool)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await _rpc(c, "tools/list")

    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"]
    assert "AEGIS_MCP_SERVER_ENABLED" in detail
    assert "disabled" in detail


async def test_auth_is_required(client):
    """Missing and wrong credentials both get the repo-standard 401."""
    missing = await _rpc(client, "tools/list", headers={})
    wrong = await _rpc(client, "tools/list", headers={"X-API-Key": "zzb9-not-the-key"})
    assert missing.status_code == 401, missing.text
    assert wrong.status_code == 401, wrong.text
    # The refusal is total: no JSON-RPC envelope leaks out of an unauthed call.
    assert "jsonrpc" not in missing.text


async def test_unknown_agent_is_404(client):
    resp = await _rpc(client, "tools/list", path=f"/api/mcp-server/{UNKNOWN_AGENT}")
    assert resp.status_code == 404, resp.text
    assert UNKNOWN_AGENT in resp.json()["detail"]


# -- protocol --------------------------------------------------------------


async def test_initialize_advertises_tools_capability(client):
    resp = await _rpc(client, "initialize", params={"protocolVersion": "2024-11-05"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 1
    result = body["result"]
    assert isinstance(result["protocolVersion"], str) and result["protocolVersion"]
    assert result["capabilities"]["tools"] == {}
    assert result["serverInfo"]["name"] == "aegis"
    assert result["serverInfo"]["version"]
    # Stateless: no session handle is minted for the client to carry.
    assert "mcp-session-id" not in {k.lower() for k in resp.headers}


async def test_notification_is_accepted_with_no_body(client):
    resp = await client.post(
        PATH,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=AUTH,
    )
    assert resp.status_code == 202, resp.text
    assert resp.content == b""


async def test_ping_returns_empty_result(client):
    resp = await _rpc(client, "ping", rpc_id="p1")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"jsonrpc": "2.0", "id": "p1", "result": {}}


async def test_unknown_method_is_method_not_found(client):
    resp = await _rpc(client, "resources/list", rpc_id=7)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == 7
    assert body["error"]["code"] == -32601
    assert "resources/list" in body["error"]["message"]
    assert "result" not in body


async def test_malformed_json_is_a_parse_error(client):
    resp = await client.post(PATH, content=b"{not json", headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["error"]["code"] == -32700
    assert body["id"] is None


async def test_get_is_405_and_delete_is_204(client):
    got = await client.get(PATH, headers=AUTH)
    deleted = await client.delete(PATH, headers=AUTH)
    assert got.status_code == 405, got.text
    assert got.headers.get("allow") == "POST, DELETE"
    assert deleted.status_code == 204, deleted.text


# -- tools/list ------------------------------------------------------------


async def test_tools_list_is_the_agents_set_minus_call_mcp_tool(client):
    resp = await _rpc(client, "tools/list")
    assert resp.status_code == 200, resp.text
    tools = resp.json()["result"]["tools"]

    assert [t["name"] for t in sorted(tools, key=lambda t: t["name"])] == [
        "capture_to_inbox",
        "whats_next",
    ]
    # Proves the filter did the work: the DB really granted call_mcp_tool.
    assert "call_mcp_tool" in TOOL_SET

    by_name = {t["name"] for t in tools}
    assert "call_mcp_tool" not in by_name

    schemas = {
        spec["function"]["name"]: spec["function"]["parameters"]
        for spec in chat_mod.CHAT_TOOLS
        if spec["function"]["name"] in by_name
    }
    for tool in tools:
        assert isinstance(tool["inputSchema"], dict)
        assert tool["inputSchema"] == schemas[tool["name"]]
        assert tool["description"]


# -- tools/call ------------------------------------------------------------


async def test_tools_call_round_trips_the_executor_result(client, monkeypatch):
    captured: dict = {}

    async def _fake(pool, args, ctx):
        captured["args"] = args
        captured["agent_id"] = ctx.agent_id
        captured["chat_context"] = ctx.chat_context
        return json.dumps({"tasks": ["ship the MCP server"]})

    monkeypatch.setitem(chat_mod.TOOL_EXECUTORS, "whats_next", _fake)

    with structlog.testing.capture_logs() as logs:
        resp = await _rpc(
            client,
            "tools/call",
            params={"name": "whats_next", "arguments": {"limit": 424242, "energy": "high"}},
        )

    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == {"tasks": ["ship the MCP server"]}
    assert result["content"][0]["type"] == "text"
    # The executor really ran, with the caller's arguments and this agent's id.
    assert captured["args"] == {"limit": 424242, "energy": "high"}
    assert captured["agent_id"] == AGENT
    assert captured["chat_context"] is None

    call_logs = [entry for entry in logs if entry.get("event") == "mcp_server_tool_call"]
    assert [entry["status"] for entry in call_logs] == ["success"]
    assert call_logs[0]["arg_keys"] == ["energy", "limit"]
    # Argument VALUES must never reach the log stream — only their keys.
    flat = json.dumps(call_logs[0])
    assert "424242" not in flat
    assert "high" not in flat


async def test_tools_call_with_invalid_args_returns_the_schema_hint(client):
    resp = await _rpc(
        client,
        "tools/call",
        # `text` is required by capture_to_inbox's schema.
        params={"name": "capture_to_inbox", "arguments": {"source": "chat"}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "error" not in body  # a bad argument is a tool result, not a protocol error
    result = body["result"]
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "text" in text and "required" in text
    assert "Expected arguments" in text  # _schema_hint's prefix — the self-correct cue


async def test_tools_call_for_an_ungranted_tool_is_invalid_params(client):
    # A real tool with a real executor, deliberately not in this agent's set.
    assert "system_status" in chat_mod.TOOL_EXECUTORS
    assert "system_status" not in TOOL_SET

    resp = await _rpc(
        client, "tools/call", params={"name": "system_status", "arguments": {}}, rpc_id=9
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == 9
    assert body["error"]["code"] == -32602
    assert "system_status" in body["error"]["message"]
    assert "result" not in body


async def test_call_mcp_tool_cannot_be_invoked_even_though_it_is_granted(client):
    """Filtered from tools/list AND refused on call — no MCP-client re-entry."""
    resp = await _rpc(
        client,
        "tools/call",
        params={"name": "call_mcp_tool", "arguments": {"server": "x", "tool": "y"}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["error"]["code"] == -32602


async def test_raising_executor_becomes_an_is_error_result_not_a_500(client, monkeypatch):
    async def _boom(pool, args, ctx):
        raise RuntimeError("zzb9 executor exploded")

    monkeypatch.setitem(chat_mod.TOOL_EXECUTORS, "whats_next", _boom)

    resp = await _rpc(client, "tools/call", params={"name": "whats_next", "arguments": {}})

    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "whats_next" in text
    assert "RuntimeError" in text
    assert "zzb9 executor exploded" in text


async def test_executor_is_restored_after_monkeypatching(client):
    """Guards the two tests above: a leaked patch would make them vacuous."""
    assert chat_mod.TOOL_EXECUTORS["whats_next"] is chat_mod._exec_whats_next


@pytest.mark.parametrize("body", [[{"jsonrpc": "2.0", "id": 1, "method": "ping"}], "nope", 5])
async def test_non_object_message_is_an_invalid_request(client, body):
    resp = await client.post(PATH, json=body, headers=AUTH)
    assert resp.status_code == 200, resp.text
    assert resp.json()["error"]["code"] == -32600
