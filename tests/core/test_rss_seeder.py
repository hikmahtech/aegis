"""Miniflux → channels seeder."""

from __future__ import annotations

import pytest
import pytest_asyncio
import respx
from aegis.services.rss_seeder import seed_rss_from_miniflux
from httpx import Response

_HEALTH_KEY = "connector_health:miniflux"


def _settings(url="https://miniflux.test", comms_url=""):
    # comms_url empty by default — connector-health alerting records state
    # but never POSTs, keeping these tests hermetic under respx.
    return type(
        "S",
        (),
        {"miniflux_url": url, "miniflux_api_key": "tok", "comms_url": comms_url, "api_key": "k"},
    )()


@pytest_asyncio.fixture(loop_scope="function", autouse=True)
async def _clean_health(db_pool):
    await db_pool.execute("DELETE FROM settings WHERE key = $1", _HEALTH_KEY)
    yield


@pytest.mark.asyncio
@respx.mock
async def test_seeder_skips_without_url(db_pool):
    count = await seed_rss_from_miniflux(db_pool, _settings(url=""))
    assert count == 0


@pytest.mark.asyncio
@respx.mock
async def test_seeder_upserts_feeds(db_pool):
    respx.get("https://miniflux.test/v1/feeds").mock(
        return_value=Response(
            200,
            json=[
                {"id": 1, "title": "Hacker News", "feed_url": "https://hnrss.org/frontpage"},
                {"id": 2, "title": "Arxiv AI", "feed_url": "https://arxiv.org/rss/cs.AI"},
            ],
        )
    )
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM channels WHERE kind='rss'")
    count = await seed_rss_from_miniflux(db_pool, _settings())
    assert count == 2
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT identifier, config, jsonb_typeof(config) AS typ "
            "FROM channels WHERE kind='rss'"
        )
    assert len(rows) == 2
    assert {r["identifier"] for r in rows} == {
        "https://hnrss.org/frontpage",
        "https://arxiv.org/rss/cs.AI",
    }
    # Every row's config must be a jsonb object — not a scalar string or array.
    # Regression guard for the double-encoding bug that turned configs into
    # arrays of stringified objects and broke list_active_channels.
    assert all(r["typ"] == "object" for r in rows)


@pytest.mark.asyncio
@respx.mock
async def test_seeder_preserves_last_cursor(db_pool):
    respx.get("https://miniflux.test/v1/feeds").mock(
        return_value=Response(
            200,
            json=[
                {"id": 1, "title": "Hacker News", "feed_url": "https://hnrss.org/frontpage"},
            ],
        )
    )
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM channels WHERE kind='rss'")

    await seed_rss_from_miniflux(db_pool, _settings())

    # Simulate ingest advancing the cursor.
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE channels SET config = jsonb_set(config, ARRAY['last_cursor'], "
            "'\"2026-04-21T00:00:00Z\"'::jsonb) WHERE kind='rss'"
        )

    # Second seed must not wipe the advanced cursor.
    await seed_rss_from_miniflux(db_pool, _settings())

    async with db_pool.acquire() as conn:
        cursor = await conn.fetchval(
            "SELECT config->>'last_cursor' FROM channels WHERE kind='rss'"
        )
    assert cursor == "2026-04-21T00:00:00Z"


@pytest.mark.asyncio
@respx.mock
async def test_seeder_records_connector_failure(db_pool):
    """A fetch failure records connector health with threshold=1 (#76)."""
    respx.get("https://miniflux.test/v1/feeds").mock(return_value=Response(500))
    count = await seed_rss_from_miniflux(db_pool, _settings())
    assert count == 0
    state = await db_pool.fetchval("SELECT value FROM settings WHERE key = $1", _HEALTH_KEY)
    assert state["consecutive_failures"] == 1
    # comms_url is unset in _settings(), so the alert can't be delivered —
    # alerted stays False and a later boot with comms configured retries.
    assert state["alerted"] is False


@pytest.mark.asyncio
@respx.mock
async def test_seeder_resets_connector_health_on_success(db_pool):
    respx.get("https://miniflux.test/v1/feeds").mock(return_value=Response(200, json=[]))
    await db_pool.execute(
        "INSERT INTO settings (key, value) VALUES ($1, $2)",
        _HEALTH_KEY,
        {"consecutive_failures": 4, "alerted": False},
    )
    await seed_rss_from_miniflux(db_pool, _settings())
    state = await db_pool.fetchval("SELECT value FROM settings WHERE key = $1", _HEALTH_KEY)
    assert state == {"consecutive_failures": 0, "alerted": False}
