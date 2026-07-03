"""GET/PUT /api/admin/agents/{id}/personality — persona editor endpoints."""

from __future__ import annotations

import base64

import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.db import run_migrations
from aegis.services import personalities as p
from httpx import ASGITransport, AsyncClient

AGENT = "zzpersona-route"


@pytest_asyncio.fixture(loop_scope="function")
async def app(test_settings, db_pool):
    await run_migrations(db_pool)
    await db_pool.execute("DELETE FROM agents WHERE id = $1", AGENT)
    await db_pool.execute(
        "INSERT INTO agents (id, name, role, system_prompt_path, active) "
        "VALUES ($1, 'Z', 'r', '', true)",
        AGENT,
    )
    p.invalidate()
    application = create_app(run_lifespan=False)
    application.dependency_overrides[get_settings] = lambda: test_settings
    application.state.db_pool = db_pool
    yield application
    await db_pool.execute("DELETE FROM agents WHERE id = $1", AGENT)
    p.invalidate()


@pytest.fixture
def auth_headers():
    creds = base64.b64encode(b"admin:admin").decode()
    return {"Authorization": f"Basic {creds}"}


async def test_personality_requires_auth(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/api/admin/agents/{AGENT}/personality")
        assert resp.status_code == 401


async def test_get_personality_returns_all_four_kinds(app, auth_headers, db_pool):
    await db_pool.execute(
        "INSERT INTO agent_personalities (agent_id, kind, content) VALUES ($1, 'soul', 'S')",
        AGENT,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/api/admin/agents/{AGENT}/personality", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == {"soul": "S", "agents": "", "user": "", "memory": ""}


async def test_put_personality_upserts_and_returns_full_persona(app, auth_headers, db_pool):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            f"/api/admin/agents/{AGENT}/personality",
            headers=auth_headers,
            json={"soul": "new soul", "memory": "new memory"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["soul"] == "new soul" and body["memory"] == "new memory"
        assert body["agents"] == "" and body["user"] == ""

        # Partial PUT updates only the given kinds.
        resp = await client.put(
            f"/api/admin/agents/{AGENT}/personality",
            headers=auth_headers,
            json={"soul": "edited"},
        )
        assert resp.status_code == 200
        assert resp.json()["soul"] == "edited"
        assert resp.json()["memory"] == "new memory"

    stored = await db_pool.fetchval(
        "SELECT content FROM agent_personalities WHERE agent_id = $1 AND kind = 'soul'",
        AGENT,
    )
    assert stored == "edited"


async def test_put_personality_unknown_kind_400(app, auth_headers):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            f"/api/admin/agents/{AGENT}/personality",
            headers=auth_headers,
            json={"vibes": "immaculate"},
        )
        assert resp.status_code == 400
        assert "unknown personality kind" in resp.json()["detail"]


async def test_personality_unknown_agent_404(app, auth_headers):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for method, kwargs in (("GET", {}), ("PUT", {"json": {"soul": "x"}})):
            resp = await client.request(
                method,
                "/api/admin/agents/zz-does-not-exist/personality",
                headers=auth_headers,
                **kwargs,
            )
            assert resp.status_code == 404
