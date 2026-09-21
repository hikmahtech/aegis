"""GET/PUT /api/admin/notes/layout, its preview, and the timezone preference —
the validating write paths for `settings.vault_layout` and
`settings.user_timezone` (the generic /api/settings editor validates nothing)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.config import Settings
from aegis.db import run_migrations
from aegis.services import vault_layout as vl
from httpx import ASGITransport, AsyncClient

_SETTINGS = {
    "database_url": "postgresql://test:test@localhost:5432/test",
    "litellm_url": "https://litellm.example.com/v1",
    "temporal_ui_url": "https://temporal.example.com",
    "admin_username": "admin",
    "admin_password": "admin",
}
AUTH = ("admin", "admin")
URL = "/api/admin/notes/layout"
TZ_URL = "/api/admin/preferences/timezone"


@pytest.fixture
def settings():
    return Settings(**_SETTINGS)


@pytest_asyncio.fixture(loop_scope="function")
async def rules_pool(db_pool):
    await run_migrations(db_pool)
    await db_pool.execute("DELETE FROM settings WHERE key IN ('vault_layout', 'user_timezone')")
    vl.invalidate_cache()
    yield db_pool
    await db_pool.execute("DELETE FROM settings WHERE key IN ('vault_layout', 'user_timezone')")
    vl.invalidate_cache()


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
    assert (await app_client.get(TZ_URL)).status_code == 401


async def test_get_returns_the_defaults_and_the_vocabularies(app_client):
    r = await app_client.get(URL, auth=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["layout"] == vl.merge({}) == body["defaults"]
    assert body["options"]["week_start"] == ["monday", "sunday"]
    assert body["options"]["indent"] == ["tab", "two_spaces", "four_spaces"]
    assert "previous" not in body["layout"]


async def test_put_persists_keeps_previous_and_get_reads_back(app_client):
    r = await app_client.put(
        URL, auth=AUTH, json={"agent_dir": "assistant", "questions_dir": "assistant/q"}
    )
    assert r.status_code == 200
    layout = r.json()["layout"]
    assert layout["agent_dir"] == "assistant"
    assert layout["previous"]["agent_dir"] == "raphael"
    assert (await app_client.get(URL, auth=AUTH)).json()["layout"] == layout


async def test_put_400s_on_a_bad_key_and_writes_nothing(app_client):
    r = await app_client.put(URL, auth=AUTH, json={"daily": {"format": "MMM YY"}})
    assert r.status_code == 400
    assert r.json()["detail"].startswith("daily.format")
    assert (await app_client.get(URL, auth=AUTH)).json()["layout"] == vl.merge({})


async def test_preview_renders_the_saved_layout_for_a_date(app_client, rules_pool):
    # The sample block names the journal's owner, so the preview shows the tag
    # the nightly run will really write. Read the owner through the capability.
    holder = await rules_pool.fetchval(
        "SELECT id FROM agents WHERE active AND capabilities @> '[\"gtd\"]'::jsonb "
        "ORDER BY id LIMIT 1"
    )
    assert holder
    r = await app_client.get(f"{URL}/preview?date=2026-09-12", auth=AUTH)
    assert r.status_code == 200
    out = r.json()
    assert out["daily"]["path"] == "journal/2026/09. Sep/12 Sep 26.md"
    assert out["week"]["label"] == "2026-W37"
    assert out["sample_block"].startswith(
        f"- #aegis/{holder} day log %% aegis:daylog:2026-09-12 %%\n\t- "
    )
    assert "Completed:" in out["sample_block"]
    assert (await app_client.get(f"{URL}/preview", auth=AUTH)).status_code == 200
    assert (await app_client.get(f"{URL}/preview?date=yesterday", auth=AUTH)).status_code == 400


async def test_preview_of_a_candidate_saves_nothing(app_client):
    candidate = {
        "daily": {"folder": "[diary/]YYYY", "format": "YYYY-MM-DD"},
        "entry": {"tag": "#me", "indent": "four_spaces"},
    }
    r = await app_client.post(
        f"{URL}/preview", auth=AUTH, json={"layout": candidate, "date": "2026-09-12"}
    )
    assert r.status_code == 200
    assert r.json()["daily"]["path"] == "diary/2026/2026-09-12.md"
    assert r.json()["sample_block"].startswith("- #me day log %% aegis:daylog:2026-09-12 %%\n    - ")
    assert (await app_client.get(URL, auth=AUTH)).json()["layout"] == vl.merge({})
    bad = await app_client.post(f"{URL}/preview", auth=AUTH, json={"layout": {"agent_dir": "a/b"}})
    assert bad.status_code == 400 and bad.json()["detail"].startswith("agent_dir")


async def test_timezone_round_trip_and_400(app_client):
    r = await app_client.get(TZ_URL, auth=AUTH)
    assert r.json() == {"timezone": "", "effective": "UTC"}
    r = await app_client.put(TZ_URL, auth=AUTH, json={"timezone": " Europe/Berlin "})
    assert r.status_code == 200
    assert r.json() == {"timezone": "Europe/Berlin", "effective": "Europe/Berlin"}
    assert (await app_client.get(TZ_URL, auth=AUTH)).json()["effective"] == "Europe/Berlin"
    r = await app_client.put(TZ_URL, auth=AUTH, json={"timezone": "Mars/Olympus"})
    assert r.status_code == 400 and "Mars/Olympus" in r.json()["detail"]
    assert (await app_client.get(TZ_URL, auth=AUTH)).json()["effective"] == "Europe/Berlin"
    r = await app_client.put(TZ_URL, auth=AUTH, json={"timezone": ""})
    assert r.json() == {"timezone": "", "effective": "UTC"}
