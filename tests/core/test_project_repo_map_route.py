"""GET/PUT /api/admin/todoist/project-repo-map — the Todoist page's field for
`project_repo_map` (#345, #558). Ships empty; the PUT validates and audits."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.config import Settings
from aegis.db import run_migrations
from httpx import ASGITransport, AsyncClient

_SETTINGS = {
    "database_url": "postgresql://test:test@localhost:5432/test",
    "litellm_url": "https://litellm.example.com/v1",
    "temporal_ui_url": "https://temporal.example.com",
    "admin_username": "admin",
    "admin_password": "admin",
}
AUTH = ("admin", "admin")
URL = "/api/admin/todoist/project-repo-map"


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    await run_migrations(db_pool)
    await db_pool.execute("DELETE FROM settings WHERE key = 'project_repo_map'")
    await db_pool.execute("DELETE FROM audit_log WHERE action = 'project_repo_map_saved'")
    yield db_pool
    await db_pool.execute("DELETE FROM settings WHERE key = 'project_repo_map'")
    await db_pool.execute("DELETE FROM audit_log WHERE action = 'project_repo_map_saved'")


@pytest_asyncio.fixture(loop_scope="function")
async def client(pool):
    app = create_app(run_lifespan=False)
    app.state.db_pool = pool
    app.state.llm = AsyncMock()
    app.dependency_overrides[get_settings] = lambda: Settings(**_SETTINGS)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_requires_auth(client):
    assert (await client.get(URL)).status_code == 401


async def test_get_is_empty_by_default(client):
    r = await client.get(URL, auth=AUTH)
    assert r.status_code == 200
    assert r.json() == {"project_repo_map": {}}


async def test_put_normalises_persists_and_audits(client, pool):
    r = await client.put(
        URL, auth=AUTH, json={"project_repo_map": {" Home Infra ": "acme/infra-gitops"}}
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"project_repo_map": {"home infra": "acme/infra-gitops"}}
    assert (await client.get(URL, auth=AUTH)).json() == {
        "project_repo_map": {"home infra": "acme/infra-gitops"}
    }
    audit = await pool.fetchrow(
        "SELECT actor, details FROM audit_log WHERE action = 'project_repo_map_saved'"
    )
    assert audit["actor"] == "admin"
    assert audit["details"] == {"project_repo_map": {"home infra": "acme/infra-gitops"}}


async def test_put_400s_on_a_bad_repo_and_writes_nothing(client, pool):
    r = await client.put(URL, auth=AUTH, json={"project_repo_map": {"home": "not a repo"}})
    assert r.status_code == 400
    assert "owner/name" in r.json()["detail"]
    assert (await client.get(URL, auth=AUTH)).json() == {"project_repo_map": {}}
    assert await pool.fetchval(
        "SELECT count(*) FROM audit_log WHERE action = 'project_repo_map_saved'"
    ) == 0
