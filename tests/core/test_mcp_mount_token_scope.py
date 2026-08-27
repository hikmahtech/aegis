"""A mount token opens ONLY the endpoint it was minted for (issue #288).

Real app, real Postgres, two real agent rows. The attack this closes is a run
reading its own mount file — which it can, by design, since an ungated run has a
shell — and then swapping the `{agent_id}` path segment to drive another agent's
tools. With one shared API key that worked; a bound token must not.
"""

from __future__ import annotations

import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.config import Settings
from aegis.services.mcp_tokens import mint_mount_token
from httpx import ASGITransport, AsyncClient

SECRET = "mount-token-test-secret"
AGENT = "zzc1-token-agent"
OTHER_AGENT = "zzc1-other-agent"
ADMIN_KEY = "test-admin-key"

TOOL_SET = ["whats_next"]


def _settings() -> Settings:
    return Settings(
        database_url="postgresql://test:test@localhost/test",
        litellm_url="https://litellm.test/v1",
        temporal_ui_url="https://temporal.test",
        n8n_ui_url="https://n8n.test",
        admin_username="admin",
        admin_password="admin",
        n8n_webhook_secret="test-secret",
        api_key=ADMIN_KEY,
        secret_key=SECRET,
        mcp_server_enabled=True,
        auth_disabled=False,
        mcp_server_allow_unauthenticated=False,
    )


@pytest_asyncio.fixture(loop_scope="function")
async def agents(db_pool):
    for agent in (AGENT, OTHER_AGENT):
        await db_pool.execute("DELETE FROM agents WHERE id = $1", agent)
        await db_pool.execute(
            "INSERT INTO agents (id, name, role, system_prompt_path, metadata, active) "
            "VALUES ($1, 'Zzc1', 'test', '', $2, true)",
            agent,
            {"tool_set": TOOL_SET},
        )
    yield
    for agent in (AGENT, OTHER_AGENT):
        await db_pool.execute("DELETE FROM agents WHERE id = $1", agent)


@pytest_asyncio.fixture(loop_scope="function")
async def client(db_pool, agents):
    app = create_app(run_lifespan=False)
    settings = _settings()
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.settings = settings
    app.state.db_pool = db_pool
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _list_tools(client, path: str, key: str):
    return await client.post(
        path,
        json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
        headers={"X-API-Key": key},
    )


async def test_a_runs_own_token_works(client):
    token = mint_mount_token(AGENT, SECRET)
    resp = await _list_tools(client, f"/api/mcp-server/{AGENT}", token)
    assert resp.status_code == 200, resp.text
    assert "result" in resp.json()


async def test_a_token_cannot_reach_another_agent(client):
    """THE issue-#288 attack: swap the path segment, keep the credential."""
    token = mint_mount_token(AGENT, SECRET)
    resp = await _list_tools(client, f"/api/mcp-server/{OTHER_AGENT}", token)
    assert resp.status_code == 403, resp.text
    assert AGENT in resp.json()["detail"]


async def test_an_ungated_token_cannot_use_the_gated_endpoint(client):
    token = mint_mount_token(AGENT, SECRET, gated=False)
    resp = await _list_tools(client, f"/api/mcp-server/{AGENT}/gated", token)
    assert resp.status_code == 403, resp.text


async def test_a_gated_token_cannot_downgrade_to_the_ungated_endpoint(client):
    """A gated run must not escape its approval gate by changing the URL."""
    token = mint_mount_token(AGENT, SECRET, gated=True)
    resp = await _list_tools(client, f"/api/mcp-server/{AGENT}", token)
    assert resp.status_code == 403, resp.text


async def test_a_gated_token_works_on_the_gated_endpoint(client):
    token = mint_mount_token(AGENT, SECRET, gated=True)
    resp = await _list_tools(client, f"/api/mcp-server/{AGENT}/gated", token)
    assert resp.status_code == 200, resp.text


async def test_an_expired_token_is_not_a_credential(client):
    """Expired reads as "not a token", so it falls through to admin auth and 401s."""
    token = mint_mount_token(AGENT, SECRET, ttl_seconds=60, now=1)
    resp = await _list_tools(client, f"/api/mcp-server/{AGENT}", token)
    assert resp.status_code == 401, resp.text


async def test_a_token_signed_with_another_secret_is_rejected(client):
    token = mint_mount_token(AGENT, "not-the-deployments-secret")
    resp = await _list_tools(client, f"/api/mcp-server/{AGENT}", token)
    assert resp.status_code == 401, resp.text


async def test_the_admin_key_still_works_everywhere(client):
    """Operator access must not be narrowed by the run-scoped path."""
    for path in (f"/api/mcp-server/{AGENT}", f"/api/mcp-server/{OTHER_AGENT}"):
        resp = await _list_tools(client, path, ADMIN_KEY)
        assert resp.status_code == 200, resp.text


async def test_a_mount_token_does_not_open_the_rest_of_the_api(client):
    """A run's credential must never authenticate the admin surface."""
    token = mint_mount_token(AGENT, SECRET)
    resp = await client.get("/api/agents", headers={"X-API-Key": token})
    assert resp.status_code == 401, resp.text
