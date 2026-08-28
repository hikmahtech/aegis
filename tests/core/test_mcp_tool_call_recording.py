"""MCP tool calls must land in `chat_tool_calls`, tagged with their mount.

The MCP surface wrote NOTHING to this table, so every coding run's and every
operator terminal's tool use was invisible to the same queries that already
covered chat — including "which tools are failing?". That blind spot is how
infra tools returning exit 127 went unnoticed for six weeks; the chat surface
at least had rows, even if they were mislabelled.

`surface` is what makes the rows useful rather than merely present: the same
tool means different things when a run calls it and when a human does.
"""

from __future__ import annotations

import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.config import Settings
from httpx import ASGITransport, AsyncClient

AGENT = "zzr1-recording-agent"
TOOL_SET = ["whats_next", "list_next_actions"]


def _settings() -> Settings:
    return Settings(
        database_url="postgresql://test:test@localhost/test",
        litellm_url="https://litellm.test/v1",
        temporal_ui_url="https://temporal.test",
        n8n_ui_url="https://n8n.test",
        admin_username="admin",
        admin_password="admin",
        n8n_webhook_secret="test-secret",
        api_key="test-key",
        secret_key="recording-test-secret",
        mcp_server_enabled=True,
        auth_disabled=True,
        mcp_server_allow_unauthenticated=True,
    )


async def _clear(db_pool):
    await db_pool.execute("DELETE FROM chat_tool_calls WHERE agent_id = $1", AGENT)


@pytest_asyncio.fixture(loop_scope="function")
async def client(db_pool):
    await _clear(db_pool)
    await db_pool.execute("DELETE FROM agents WHERE id = $1", AGENT)
    await db_pool.execute(
        "INSERT INTO agents (id, name, role, system_prompt_path, metadata, active) "
        "VALUES ($1, 'Zzr1', 'test', '', $2, true)",
        AGENT,
        {"tool_set": TOOL_SET},
    )
    app = create_app(run_lifespan=False)
    settings = _settings()
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.settings = settings
    app.state.db_pool = db_pool
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await _clear(db_pool)
    await db_pool.execute("DELETE FROM agents WHERE id = $1", AGENT)


async def _call(client, tool: str, args: dict | None = None, path_suffix: str = ""):
    return await client.post(
        f"/api/mcp-server/{AGENT}{path_suffix}",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args or {}},
        },
        headers={"X-API-Key": "test-key"},
    )


async def _rows(db_pool):
    return await db_pool.fetch(
        "SELECT tool_name, status, surface FROM chat_tool_calls "
        "WHERE agent_id = $1 ORDER BY created_at",
        AGENT,
    )


async def test_a_tool_call_is_recorded_with_the_mcp_surface(client, db_pool):
    resp = await _call(client, "whats_next")
    assert resp.status_code == 200, resp.text
    rows = await _rows(db_pool)
    assert len(rows) == 1, "the MCP surface recorded nothing"
    assert rows[0]["tool_name"] == "whats_next"
    assert rows[0]["surface"] == "mcp"


async def test_the_operator_mount_is_recorded_as_its_own_surface(client, db_pool):
    """A human's terminal must be distinguishable from a run's mount."""
    resp = await _call(client, "whats_next", path_suffix="/operator")
    assert resp.status_code == 200, resp.text
    rows = await _rows(db_pool)
    assert [r["surface"] for r in rows] == ["mcp_operator"]


async def test_a_tool_not_in_the_tool_set_is_recorded_as_not_granted(client, db_pool):
    """An ungranted tool is a real signal — someone's config expects it."""
    resp = await _call(client, "restart_service", {"context": "swarm", "service_name": "x"})
    assert resp.status_code == 200, resp.text
    rows = await _rows(db_pool)
    assert [(r["tool_name"], r["status"]) for r in rows] == [
        ("restart_service", "not_granted")
    ]


async def test_bad_arguments_are_recorded_as_invalid_args(client, db_pool):
    resp = await _call(client, "list_next_actions", {"limit": "not-an-integer"})
    assert resp.status_code == 200, resp.text
    rows = await _rows(db_pool)
    assert rows and rows[0]["status"] == "invalid_args"


async def test_the_approval_tool_is_not_recorded_as_a_tool_call(client, db_pool):
    """It is the gate mechanism, not a tool the agent chose to call, and its
    outcome already lives in `interactions`."""
    await _call(client, "approve_tool_use", {"tool_name": "x", "input": {}})
    rows = await _rows(db_pool)
    assert [r["tool_name"] for r in rows] == []
