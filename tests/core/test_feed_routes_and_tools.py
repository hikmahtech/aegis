"""The feed admin routes and Raphael's feed tools (#511, #512)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest_asyncio
from aegis.api.auth import verify_auth
from aegis.api.routes import channels
from aegis.services import feeds
from aegis.services.chat import TOOL_EXECUTORS, ToolContext
from aegis.services.tools.registry import TOOL_REGISTRY
from fastapi import FastAPI

_PREFIX = "https://zzfeedroute.test/"


@pytest_asyncio.fixture(loop_scope="function")
async def client(db_pool):
    app = FastAPI()
    app.include_router(channels.router)
    app.dependency_overrides[verify_auth] = lambda: True
    app.state.db_pool = db_pool
    await db_pool.execute("DELETE FROM channels WHERE identifier LIKE $1", f"{_PREFIX}%")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c
    await db_pool.execute("DELETE FROM channels WHERE identifier LIKE $1", f"{_PREFIX}%")


async def test_feed_stats_route_lists_every_rss_channel(client, db_pool):
    cid = await db_pool.fetchval(
        "INSERT INTO channels (kind, identifier, config) VALUES ('rss', $1, $2) RETURNING id::text",
        _PREFIX + "a",
        {"label": "Route feed", "ingest": "abstract"},
    )
    r = await client.get("/api/admin/channels/feed-stats")
    assert r.status_code == 200, r.text
    row = next(f for f in r.json() if f["id"] == cid)
    assert row["label"] == "Route feed"
    assert row["ingest"] == "abstract"
    assert row["used_90d"] == 0


async def test_retention_preview_route_is_a_dry_run_and_validates_its_window(client):
    ok = await client.get("/api/admin/channels/retention-preview?older_than_days=30")
    assert ok.status_code == 200, ok.text
    assert ok.json()["dry_run"] is True
    assert ok.json()["older_than_days"] == 30
    bad = await client.get("/api/admin/channels/retention-preview?older_than_days=0")
    assert bad.status_code == 422


# --------------------------------------------------------------------------
# The three tools
# --------------------------------------------------------------------------


def test_the_feed_tools_are_registered_and_dispatched():
    for name in ("list_feeds", "subscribe_feed", "unsubscribe_feed"):
        assert name in TOOL_REGISTRY
        assert name in TOOL_EXECUTORS
    assert TOOL_REGISTRY["subscribe_feed"].parameters["required"] == ["url"]
    assert TOOL_REGISTRY["unsubscribe_feed"].parameters["required"] == ["feed"]


async def test_subscribe_feed_files_the_feed_under_the_calling_agent(db_pool):
    fake = AsyncMock(return_value={"status": "subscribed", "label": "X"})
    with patch.object(feeds, "subscribe", fake):
        out = await TOOL_EXECUTORS["subscribe_feed"](
            db_pool, {"url": "https://x.test/feed", "label": "X"}, ToolContext(agent_id="raphael")
        )
    assert json.loads(out)["status"] == "subscribed"
    fake.assert_awaited_once_with(db_pool, "https://x.test/feed", label="X", agent_id="raphael")


async def test_unsubscribe_feed_passes_the_name_through(db_pool):
    fake = AsyncMock(return_value={"status": "unsubscribed"})
    with patch.object(feeds, "unsubscribe", fake):
        out = await TOOL_EXECUTORS["unsubscribe_feed"](
            db_pool, {"feed": "arxiv-ai"}, ToolContext(agent_id="raphael")
        )
    assert json.loads(out)["status"] == "unsubscribed"
    fake.assert_awaited_once_with(db_pool, "arxiv-ai")


async def test_list_feeds_returns_the_numbers_for_each_feed(db_pool):
    await db_pool.execute("DELETE FROM channels WHERE identifier LIKE $1", f"{_PREFIX}%")
    await db_pool.execute(
        "INSERT INTO channels (kind, identifier, config) VALUES ('rss', $1, $2)",
        _PREFIX + "listed",
        {"label": "Listed feed"},
    )
    try:
        out = json.loads(await TOOL_EXECUTORS["list_feeds"](db_pool, {}, ToolContext()))
    finally:
        await db_pool.execute("DELETE FROM channels WHERE identifier LIKE $1", f"{_PREFIX}%")
    row = next(f for f in out["feeds"] if f["label"] == "Listed feed")
    assert set(row) >= {"used_30d", "used_90d", "entries_30d", "fetch_failures", "ingest"}
