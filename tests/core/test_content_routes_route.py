"""Tests for /api/admin/todoist/content-routes — CRUD + preview + suggest.

Real Postgres (matching test_todoist_routes.py) so the SQL is exercised; the LLM
is mocked for the suggest endpoint.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
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


@pytest.fixture
def settings():
    return Settings(**_SETTINGS)


@pytest_asyncio.fixture(loop_scope="function")
async def routes_pool(db_pool):
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM settings WHERE key='content_routes'")
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ('todoist_managed_project_ids', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            {"inbox": "P_CR_INBOX"},
        )
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, raw) "
            "VALUES ('P_CR_INBOX','Inbox',true,'{}'::jsonb) ON CONFLICT (id) DO NOTHING"
        )
        await conn.execute("DELETE FROM todoist_tasks WHERE id IN ('CR1','CR2')")
        await conn.execute(
            "INSERT INTO todoist_tasks (id, project_id, content, labels, raw) VALUES "
            "('CR1','P_CR_INBOX','APP-1: broken thing', ARRAY[]::text[], '{}'::jsonb), "
            "('CR2','P_CR_INBOX','buy milk', ARRAY[]::text[], '{}'::jsonb)"
        )
    yield db_pool
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM settings WHERE key='content_routes'")
        await conn.execute("DELETE FROM todoist_tasks WHERE id IN ('CR1','CR2')")


@pytest_asyncio.fixture(loop_scope="function")
async def app_client(settings, routes_pool):
    app = create_app(run_lifespan=False)
    app.state.db_pool = routes_pool
    app.state.llm = AsyncMock()
    app.dependency_overrides[get_settings] = lambda: settings
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, app


async def test_content_routes_requires_auth(app_client):
    client, _ = app_client
    assert (await client.get("/api/admin/todoist/content-routes")).status_code == 401


async def test_get_empty_then_put_and_get(app_client):
    client, _ = app_client
    r = await client.get("/api/admin/todoist/content-routes", auth=AUTH)
    assert r.status_code == 200
    assert r.json()["routes"] == []
    assert "regex" in r.json()["match_modes"]

    put = await client.put(
        "/api/admin/todoist/content-routes",
        auth=AUTH,
        json={"routes": [{"key": "app", "match": "prefix", "value": "APP-", "assignee": "@pandora"}]},
    )
    assert put.status_code == 200
    assert put.json()["routes"][0]["key"] == "app"
    got = await client.get("/api/admin/todoist/content-routes", auth=AUTH)
    assert got.json()["routes"][0]["value"] == "APP-"


async def test_put_bad_regex_400(app_client):
    client, _ = app_client
    r = await client.put(
        "/api/admin/todoist/content-routes",
        auth=AUTH,
        json={"routes": [{"key": "bad", "match": "regex", "value": "("}]},
    )
    assert r.status_code == 400


async def test_preview_matches_inbox(app_client):
    client, _ = app_client
    r = await client.post(
        "/api/admin/todoist/content-routes/preview",
        auth=AUTH,
        json={"match": "prefix", "value": "APP-"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["match_count"] == 1
    assert body["matches"] == ["APP-1: broken thing"]


async def test_preview_bad_pattern_400(app_client):
    client, _ = app_client
    r = await client.post(
        "/api/admin/todoist/content-routes/preview",
        auth=AUTH,
        json={"match": "regex", "value": "("},
    )
    assert r.status_code == 400


async def test_suggest_returns_pattern(app_client):
    client, app = app_client
    # JSON text: {"pattern": "^APP-\\d+:"} → parsed pattern = ^APP-\d+:
    app.state.llm.think = AsyncMock(return_value={"response": '{"pattern": "^APP-\\\\d+:"}'})
    r = await client.post(
        "/api/admin/todoist/content-routes/suggest",
        auth=AUTH,
        json={"examples": ["APP-1: x", "APP-99: y"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["pattern"] == r"^APP-\d+:"
    assert body["all_examples_match"] is True


async def test_suggest_empty_examples_400(app_client):
    client, _ = app_client
    r = await client.post(
        "/api/admin/todoist/content-routes/suggest", auth=AUTH, json={"examples": []}
    )
    assert r.status_code == 400
