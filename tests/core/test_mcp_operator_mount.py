"""The operator mount: a human's terminal, never a run's (PR3 of the fabric work).

The asymmetry under test is the whole design. A run holds a mount token and can
read its own config file; the operator mount needs a credential that is never
written to the coding host. So a run must not be able to escalate from "use my
tools" to "start and stop runs", even though it can read everything it has.

`auth_disabled=True` throughout on purpose: that is the live posture, and the
operator gate has to hold in spite of it.
"""

from __future__ import annotations

import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.config import Settings
from aegis.services.mcp_tokens import mint_mount_token
from httpx import ASGITransport, AsyncClient

SECRET = "operator-mount-test-secret"
ADMIN_KEY = "operator-admin-key"
AGENT = "zzd1-operator-agent"

# Granted in the DB so the ungated mount must strip them and the operator
# mount must keep them.
TOOL_SET = ["whats_next", "dispatch_agent_run", "stop_agent_run", "call_mcp_tool"]


def _settings(**over) -> Settings:
    base = {
        "database_url": "postgresql://test:test@localhost/test",
        "litellm_url": "https://litellm.test/v1",
        "temporal_ui_url": "https://temporal.test",
        "n8n_ui_url": "https://n8n.test",
        "admin_username": "admin",
        "admin_password": "admin",
        "n8n_webhook_secret": "test-secret",
        "api_key": ADMIN_KEY,
        "secret_key": SECRET,
        "mcp_server_enabled": True,
        "auth_disabled": True,
        "mcp_server_allow_unauthenticated": True,
    }
    base.update(over)
    return Settings(**base)


@pytest_asyncio.fixture(loop_scope="function")
async def agent_row(db_pool):
    # The MCP surface records tool calls, a FK child of `agents`.
    await db_pool.execute("DELETE FROM chat_tool_calls WHERE agent_id = $1", AGENT)
    await db_pool.execute("DELETE FROM agents WHERE id = $1", AGENT)
    await db_pool.execute(
        "INSERT INTO agents (id, name, role, system_prompt_path, metadata, active) "
        "VALUES ($1, 'Zzd1', 'test', '', $2, true)",
        AGENT,
        {"tool_set": TOOL_SET},
    )
    yield AGENT
    # The MCP surface records tool calls, a FK child of `agents`.
    await db_pool.execute("DELETE FROM chat_tool_calls WHERE agent_id = $1", AGENT)
    await db_pool.execute("DELETE FROM agents WHERE id = $1", AGENT)


@pytest_asyncio.fixture(loop_scope="function")
async def client(db_pool, agent_row):
    app = create_app(run_lifespan=False)
    settings = _settings()
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.settings = settings
    app.state.db_pool = db_pool
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _list(client, path, key=None):
    headers = {"X-API-Key": key} if key else {}
    return await client.post(
        path, json={"jsonrpc": "2.0", "method": "tools/list", "id": 1}, headers=headers
    )


def _names(resp):
    return {t["name"] for t in resp.json()["result"]["tools"]}


# ── the gate ──────────────────────────────────────────────────────────────


async def test_operator_mount_refuses_a_run_mount_token(client):
    """The escalation this endpoint exists to prevent."""
    token = mint_mount_token(AGENT, SECRET)
    resp = await _list(client, f"/api/mcp-server/{AGENT}/operator", token)
    assert resp.status_code == 403, resp.text
    assert "mount token" in resp.json()["detail"].lower()


async def test_operator_mount_refuses_no_credential_despite_auth_disabled(client):
    """auth_disabled opens every other route here; it must not open this one."""
    resp = await _list(client, f"/api/mcp-server/{AGENT}/operator")
    assert resp.status_code == 401, resp.text
    assert "AEGIS_AUTH_DISABLED" in resp.json()["detail"]


async def test_operator_mount_refuses_a_wrong_key(client):
    resp = await _list(client, f"/api/mcp-server/{AGENT}/operator", "not-the-key")
    assert resp.status_code == 401, resp.text


async def test_operator_mount_accepts_the_admin_key(client):
    resp = await _list(client, f"/api/mcp-server/{AGENT}/operator", ADMIN_KEY)
    assert resp.status_code == 200, resp.text


# ── the served surface ────────────────────────────────────────────────────


async def test_operator_mount_serves_the_run_spawning_tools(client):
    resp = await _list(client, f"/api/mcp-server/{AGENT}/operator", ADMIN_KEY)
    names = _names(resp)
    assert "dispatch_agent_run" in names
    assert "stop_agent_run" in names


async def test_operator_mount_still_withholds_the_mcp_passthrough(client):
    """Confused-deputy risk does not depend on who opened the door."""
    resp = await _list(client, f"/api/mcp-server/{AGENT}/operator", ADMIN_KEY)
    assert "call_mcp_tool" not in _names(resp)


async def test_the_run_mount_still_withholds_run_spawning_tools(client):
    """Falsifiability control: the recursion guard is untouched on a run mount."""
    token = mint_mount_token(AGENT, SECRET)
    resp = await _list(client, f"/api/mcp-server/{AGENT}", token)
    assert resp.status_code == 200, resp.text
    names = _names(resp)
    assert "dispatch_agent_run" not in names
    assert "stop_agent_run" not in names
    assert "call_mcp_tool" not in names
