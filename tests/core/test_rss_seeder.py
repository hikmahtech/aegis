"""Miniflux → channels seeder."""

from __future__ import annotations

import pytest
import respx
from aegis.services.rss_seeder import seed_rss_from_miniflux
from httpx import Response


@pytest.mark.asyncio
@respx.mock
async def test_seeder_skips_without_url(db_pool):
    count = await seed_rss_from_miniflux(db_pool, "", "api-key")
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
    count = await seed_rss_from_miniflux(db_pool, "https://miniflux.test", "tok")
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

    await seed_rss_from_miniflux(db_pool, "https://miniflux.test", "tok")

    # Simulate ingest advancing the cursor.
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE channels SET config = jsonb_set(config, ARRAY['last_cursor'], "
            "'\"2026-04-21T00:00:00Z\"'::jsonb) WHERE kind='rss'"
        )

    # Second seed must not wipe the advanced cursor.
    await seed_rss_from_miniflux(db_pool, "https://miniflux.test", "tok")

    async with db_pool.acquire() as conn:
        cursor = await conn.fetchval(
            "SELECT config->>'last_cursor' FROM channels WHERE kind='rss'"
        )
    assert cursor == "2026-04-21T00:00:00Z"
