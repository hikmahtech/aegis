"""track_topic / untrack_topic chat tools — a thin shell over
`research_topics` (#513). Real test database."""

import json

import pytest
import pytest_asyncio
from aegis.services import research_topics
from aegis.services.chat import ToolContext, _exec_track_topic, _exec_untrack_topic
from aegis.services.hub import get_problem

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    await db_pool.execute("DELETE FROM settings WHERE key = $1", research_topics.TOPICS_SETTING)
    yield db_pool
    await db_pool.execute("DELETE FROM settings WHERE key = $1", research_topics.TOPICS_SETTING)


@pytest.fixture
def ctx():
    return ToolContext(agent_id="raphael")


async def _registry(pool) -> list[dict]:
    value = await pool.fetchval(
        "SELECT value FROM settings WHERE key = $1", research_topics.TOPICS_SETTING
    )
    return (value or {}).get("topics", [])


async def test_track_topic_adds_new_and_opens_a_round(pool, ctx):
    data = json.loads(
        await _exec_track_topic(pool, {"topic_name": "crypto", "queries": ["bitcoin", "ethereum"]}, ctx)
    )
    assert data["status"] == "added"
    assert data["topic"] == "crypto"
    assert data["query_count"] == 2
    assert data["total_topics"] == 1
    assert (await get_problem(pool, data["problem_id"]))["class"] == "topic"


async def test_track_topic_updates_existing_case_insensitively(pool, ctx):
    await _exec_track_topic(pool, {"topic_name": "AI Safety", "queries": ["alignment"]}, ctx)
    data = json.loads(
        await _exec_track_topic(
            pool, {"topic_name": "ai safety", "queries": ["alignment", "RLHF"]}, ctx
        )
    )
    assert data["status"] == "updated"
    assert data["total_topics"] == 1
    assert (await _registry(pool))[0]["queries"] == ["alignment", "RLHF"]


async def test_track_topic_preserves_other_topics(pool, ctx):
    for name in ("ai", "crypto", "climate"):
        await _exec_track_topic(pool, {"topic_name": name, "queries": [name]}, ctx)
    assert [t["name"] for t in await _registry(pool)] == ["ai", "crypto", "climate"]


async def test_track_topic_priority_default_and_explicit(pool, ctx):
    await _exec_track_topic(pool, {"topic_name": "geo", "queries": ["BRICS"]}, ctx)
    await _exec_track_topic(
        pool, {"topic_name": "urgent", "queries": ["critical"], "priority": "high"}, ctx
    )
    assert [t["priority"] for t in await _registry(pool)] == ["medium", "high"]


@pytest.mark.parametrize("args", [{"queries": ["test"]}, {"topic_name": "test", "queries": []}])
async def test_track_topic_refuses_a_topic_without_name_or_queries(pool, ctx, args):
    data = json.loads(await _exec_track_topic(pool, args, ctx))
    assert data == {"error": "topic_name and queries are required"}
    assert await _registry(pool) == []


async def test_untrack_topic_drops_it_and_closes_its_round(pool, ctx):
    tracked = json.loads(
        await _exec_track_topic(pool, {"topic_name": "rust", "queries": ["rust"]}, ctx)
    )
    # `@aegis_tool` executors take `(pool, args, ctx)`, like the hand-written ones.
    data = json.loads(await _exec_untrack_topic(pool, {"topic_name": "Rust"}, ctx))
    assert data["status"] == "removed" and data["round_closed"] is True
    assert await _registry(pool) == []
    assert (await get_problem(pool, tracked["problem_id"]))["closed_at"] is not None
    missing = json.loads(await _exec_untrack_topic(pool, {"topic_name": "rust"}, ctx))
    assert missing["status"] == "not_found"
