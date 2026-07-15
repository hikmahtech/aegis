"""Agent lifecycle endpoints — reassign owned rows, then delete (issue #44)."""

import base64

import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def auth_headers():
    creds = base64.b64encode(b"admin:admin").decode()
    return {"Authorization": f"Basic {creds}"}


@pytest_asyncio.fixture(loop_scope="function")
async def app(test_settings, db_pool):
    application = create_app(run_lifespan=False)
    application.dependency_overrides[get_settings] = lambda: test_settings
    application.state.db_pool = db_pool
    return application


@pytest_asyncio.fixture(loop_scope="function")
async def seeded(db_pool):
    """Two throwaway agents; agent a owns an activity and a memory row."""

    async def _cleanup():
        await db_pool.execute("DELETE FROM activities WHERE slug LIKE 'test-life-%'")
        await db_pool.execute(
            "DELETE FROM agent_memory WHERE agent_id IN ('test-life-a','test-life-b')"
        )
        await db_pool.execute("DELETE FROM agents WHERE id IN ('test-life-a','test-life-b')")

    await _cleanup()
    await db_pool.execute(
        "INSERT INTO agents (id, name, role, system_prompt_path) VALUES "
        "('test-life-a','Life A','r',''),('test-life-b','Life B','r','')"
    )
    await db_pool.execute(
        "INSERT INTO activities (slug, workflow_type, agent_id, schedule_cron) "
        "VALUES ('test-life-act','NopFlow','test-life-a','0 0 * * *')"
    )
    await db_pool.execute(
        "INSERT INTO agent_memory (agent_id, content) VALUES ('test-life-a','remember')"
    )
    yield
    await _cleanup()


async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_delete_blocked_while_rows_exist(app, auth_headers, seeded):
    async with await _client(app) as client:
        resp = await client.delete("/api/agents/test-life-a", headers=auth_headers)
        assert resp.status_code == 409
        assert "reassign" in resp.json()["detail"]


async def test_reassign_moves_rows_then_delete_succeeds(app, auth_headers, seeded, db_pool):
    async with await _client(app) as client:
        resp = await client.post(
            "/api/agents/test-life-a/reassign", json={"to": "test-life-b"}, headers=auth_headers
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["reassigned"] == {"activities": 1, "agent_memory": 1}
        assert body["total"] == 2
        owner = await db_pool.fetchval("SELECT agent_id FROM activities WHERE slug='test-life-act'")
        assert owner == "test-life-b"

        resp = await client.delete("/api/agents/test-life-a", headers=auth_headers)
        assert resp.status_code == 204
        assert await db_pool.fetchval("SELECT id FROM agents WHERE id='test-life-a'") is None


async def test_reassign_validation(app, auth_headers, seeded):
    async with await _client(app) as client:
        resp = await client.post(
            "/api/agents/test-life-a/reassign", json={"to": "test-life-a"}, headers=auth_headers
        )
        assert resp.status_code == 400
        resp = await client.post(
            "/api/agents/test-life-a/reassign", json={}, headers=auth_headers
        )
        assert resp.status_code == 400
        resp = await client.post(
            "/api/agents/test-life-a/reassign", json={"to": "no-such"}, headers=auth_headers
        )
        assert resp.status_code == 404
        resp = await client.post(
            "/api/agents/no-such/reassign", json={"to": "test-life-b"}, headers=auth_headers
        )
        assert resp.status_code == 404


async def test_delete_system_refused_and_unknown_404(app, auth_headers):
    async with await _client(app) as client:
        resp = await client.delete("/api/agents/system", headers=auth_headers)
        assert resp.status_code == 400
        resp = await client.delete("/api/agents/never-existed", headers=auth_headers)
        assert resp.status_code == 404
