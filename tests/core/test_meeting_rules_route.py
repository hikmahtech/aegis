"""GET/PUT /api/admin/email/meeting-rules — the validating write path for
settings.meeting_rules (the generic /api/settings editor validates nothing)."""

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
URL = "/api/admin/email/meeting-rules"


@pytest.fixture
def settings():
    return Settings(**_SETTINGS)


@pytest_asyncio.fixture(loop_scope="function")
async def rules_pool(db_pool):
    await run_migrations(db_pool)
    await db_pool.execute("DELETE FROM settings WHERE key='meeting_rules'")
    yield db_pool
    await db_pool.execute("DELETE FROM settings WHERE key='meeting_rules'")


@pytest_asyncio.fixture(loop_scope="function")
async def app_client(settings, rules_pool):
    app = create_app(run_lifespan=False)
    app.state.db_pool = rules_pool
    app.state.llm = AsyncMock()
    app.dependency_overrides[get_settings] = lambda: settings
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_requires_auth(app_client):
    assert (await app_client.get(URL)).status_code == 401


async def test_get_returns_empty_defaults(app_client):
    r = await app_client.get(URL, auth=AUTH)
    assert r.status_code == 200
    assert r.json() == {"self_names": []}


async def test_put_persists_and_get_reads_back(app_client):
    r = await app_client.put(URL, auth=AUTH, json={"self_names": ["Sam Doe", " Sam "]})
    assert r.status_code == 200
    assert r.json() == {"self_names": ["Sam Doe", "Sam"]}
    assert (await app_client.get(URL, auth=AUTH)).json() == {"self_names": ["Sam Doe", "Sam"]}


async def test_put_400s_on_bad_shape_instead_of_silently_dropping(app_client):
    r = await app_client.put(URL, auth=AUTH, json={"self_names": "Sam"})
    assert r.status_code == 400
    assert "self_names" in r.json()["detail"]
    # Nothing was written.
    assert (await app_client.get(URL, auth=AUTH)).json() == {"self_names": []}
