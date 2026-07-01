"""Tests for /api/admin/todoist/state — real Postgres so the SQL is exercised
(mocked-pool route tests have repeatedly hidden schema drift in this repo).
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.config import Settings
from aegis.db import run_migrations
from httpx import ASGITransport, AsyncClient

_TEST_REQUIRED_SETTINGS = {
    "database_url": "postgresql://test:test@localhost:5432/test",
    "litellm_url": "https://litellm.example.com/v1",
    "temporal_ui_url": "https://temporal.example.com",
    "admin_username": "admin",
    "admin_password": "admin",
}


@pytest.fixture
def settings():
    return Settings(**_TEST_REQUIRED_SETTINGS)


@pytest_asyncio.fixture(loop_scope="function")
async def seeded_pool(db_pool):
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM todoist_outbox WHERE temp_id LIKE 'tr-%'")
        await conn.execute(
            "INSERT INTO todoist_outbox (temp_id, command, status, attempt_count, last_attempt_at) "
            "VALUES ('tr-failed-1', '{\"type\": \"item_add\"}'::jsonb, 'failed', 5, now()), "
            "       ('tr-pending-1', '{\"type\": \"item_update\"}'::jsonb, 'pending', 1, now())"
        )
    return db_pool


@pytest_asyncio.fixture(loop_scope="function")
async def client(settings, seeded_pool):
    app = create_app(run_lifespan=False)
    app.state.db_pool = seeded_pool
    app.dependency_overrides[get_settings] = lambda: settings
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_todoist_state_requires_auth(client):
    resp = await client.get("/api/admin/todoist/state")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_todoist_state_shape_and_failed_outbox(client):
    resp = await client.get("/api/admin/todoist/state", auth=("admin", "admin"))
    assert resp.status_code == 200
    body = resp.json()

    # Sync watermarks row exists from migration seed (key='main')
    assert body["sync"] is not None
    assert body["sync"]["key"] == "main"

    # Outbox counts include our seeded failed + pending rows
    assert body["outbox"]["counts"].get("failed", 0) >= 1
    assert body["outbox"]["counts"].get("pending", 0) >= 1
    assert body["outbox"]["oldest_pending_age_seconds"] is not None

    # The failed row is listed with its command type — this is the surface
    # that makes silently-lost Todoist writes visible.
    types = [r["command_type"] for r in body["outbox"]["failed_recent"]]
    assert "item_add" in types

    # Task counters are present and integers
    assert isinstance(body["tasks"]["open"], int)
    assert isinstance(body["tasks"]["completed_7d"], int)
    assert isinstance(body["tasks"]["pending_clarify"], int)
