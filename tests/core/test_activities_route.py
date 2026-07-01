"""Route tests for the /admin/activities flow-config API."""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from aegis.api.auth import verify_auth
from aegis.api.routes import activities
from fastapi import FastAPI

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(loop_scope="function")
async def client(db_pool):
    app = FastAPI()
    app.include_router(activities.router)
    app.dependency_overrides[verify_auth] = lambda: True
    app.state.db_pool = db_pool
    await db_pool.execute("DELETE FROM activities WHERE slug = 'test-flow-cfg'")
    await db_pool.execute(
        "INSERT INTO activities (slug, workflow_type, agent_id, schedule_cron, config, active) "
        "VALUES ('test-flow-cfg', 'TestFlow', 'raphael', '0 * * * *', '{}'::jsonb, true)"
    )
    transport = httpx.ASGITransport(app=app)
    yield httpx.AsyncClient(transport=transport, base_url="http://t")
    await db_pool.execute("DELETE FROM activities WHERE slug = 'test-flow-cfg'")


async def test_list_includes_the_activity(client):
    async with client:
        r = await client.get("/api/admin/activities")
    assert r.status_code == 200
    assert any(a["slug"] == "test-flow-cfg" for a in r.json())


async def test_patch_config_active_and_schedule(client):
    async with client:
        r = await client.patch(
            "/api/admin/activities/test-flow-cfg",
            json={"active": False, "schedule_cron": "15 */4 * * *", "config": {"folder_id": "abc123"}},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["active"] is False
        assert body["schedule_cron"] == "15 */4 * * *"
        assert body["config"] == {"folder_id": "abc123"}
        # the change persisted
        again = (await client.get("/api/admin/activities")).json()
        row = next(a for a in again if a["slug"] == "test-flow-cfg")
        assert row["config"]["folder_id"] == "abc123"


async def test_patch_unknown_slug_404(client):
    async with client:
        r = await client.patch("/api/admin/activities/does-not-exist", json={"active": True})
    assert r.status_code == 404


async def test_patch_empty_body_400(client):
    async with client:
        r = await client.patch("/api/admin/activities/test-flow-cfg", json={})
    assert r.status_code == 400
