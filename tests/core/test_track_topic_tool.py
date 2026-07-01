"""Tests for track_topic chat tool."""

import json
from unittest.mock import AsyncMock

import pytest
from aegis.services.chat import ToolContext, _exec_track_topic


@pytest.fixture
def pool():
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=None)
    pool.execute = AsyncMock()
    return pool


@pytest.fixture
def ctx():
    return ToolContext(agent_id="sebas")


async def test_track_topic_adds_new(pool, ctx):
    pool.fetchrow = AsyncMock(
        return_value={"value": {"topics": [{"name": "ai", "queries": ["AI"], "priority": "high"}]}}
    )

    result = await _exec_track_topic(
        pool, {"topic_name": "crypto", "queries": ["bitcoin", "ethereum"]}, ctx
    )
    data = json.loads(result)
    assert data["status"] == "added"
    assert data["topic"] == "crypto"
    assert data["query_count"] == 2
    assert data["total_topics"] == 2

    pool.execute.assert_called_once()


async def test_track_topic_updates_existing(pool, ctx):
    pool.fetchrow = AsyncMock(
        return_value={"value": {"topics": [{"name": "ai", "queries": ["AI"], "priority": "high"}]}}
    )

    result = await _exec_track_topic(
        pool, {"topic_name": "ai", "queries": ["AI", "LLM", "AGI"]}, ctx
    )
    data = json.loads(result)
    assert data["status"] == "updated"
    assert data["topic"] == "ai"
    assert data["query_count"] == 3
    assert data["total_topics"] == 1  # still 1 topic, just updated


async def test_track_topic_no_existing_settings(pool, ctx):
    pool.fetchrow = AsyncMock(return_value=None)

    result = await _exec_track_topic(pool, {"topic_name": "new", "queries": ["test"]}, ctx)
    data = json.loads(result)
    assert data["status"] == "added"
    assert data["topic"] == "new"
    assert data["total_topics"] == 1

    pool.execute.assert_called_once()


async def test_track_topic_empty_topics_in_settings(pool, ctx):
    """Handle settings row with empty topics list."""
    pool.fetchrow = AsyncMock(return_value={"value": {"topics": []}})

    result = await _exec_track_topic(
        pool, {"topic_name": "climate", "queries": ["climate change"]}, ctx
    )
    data = json.loads(result)
    assert data["status"] == "added"
    assert data["total_topics"] == 1


async def test_track_topic_priority_default(pool, ctx):
    """Priority defaults to medium when not specified."""
    pool.fetchrow = AsyncMock(return_value=None)

    await _exec_track_topic(pool, {"topic_name": "geopolitics", "queries": ["BRICS"]}, ctx)
    call_args = pool.execute.call_args
    # The JSON payload should include priority medium
    payload = call_args[0][1]
    topic = payload["topics"][0]
    assert topic["priority"] == "medium"


async def test_track_topic_priority_high(pool, ctx):
    """Explicit priority is preserved."""
    pool.fetchrow = AsyncMock(return_value=None)

    await _exec_track_topic(
        pool, {"topic_name": "urgent", "queries": ["critical"], "priority": "high"}, ctx
    )
    call_args = pool.execute.call_args
    payload = call_args[0][1]
    topic = payload["topics"][0]
    assert topic["priority"] == "high"


async def test_track_topic_missing_topic_name(pool, ctx):
    result = await _exec_track_topic(pool, {"queries": ["test"]}, ctx)
    data = json.loads(result)
    assert "error" in data

    pool.execute.assert_not_called()


async def test_track_topic_missing_queries(pool, ctx):
    result = await _exec_track_topic(pool, {"topic_name": "test", "queries": []}, ctx)
    data = json.loads(result)
    assert "error" in data

    pool.execute.assert_not_called()


async def test_track_topic_case_insensitive_update(pool, ctx):
    """Topic name matching is case-insensitive."""
    pool.fetchrow = AsyncMock(
        return_value={
            "value": {
                "topics": [{"name": "AI Safety", "queries": ["alignment"], "priority": "medium"}]
            }
        }
    )

    result = await _exec_track_topic(
        pool, {"topic_name": "ai safety", "queries": ["alignment", "RLHF"]}, ctx
    )
    data = json.loads(result)
    assert data["status"] == "updated"
    assert data["total_topics"] == 1


async def test_track_topic_preserves_other_topics(pool, ctx):
    """Adding a new topic doesn't remove existing ones."""
    existing_topics = [
        {"name": "ai", "queries": ["AI"], "priority": "high"},
        {"name": "crypto", "queries": ["bitcoin"], "priority": "medium"},
    ]
    pool.fetchrow = AsyncMock(return_value={"value": {"topics": existing_topics}})

    result = await _exec_track_topic(
        pool, {"topic_name": "climate", "queries": ["global warming"]}, ctx
    )
    data = json.loads(result)
    assert data["status"] == "added"
    assert data["total_topics"] == 3

    # Verify all 3 topics in the write payload
    call_args = pool.execute.call_args
    payload = call_args[0][1]
    topic_names = [t["name"] for t in payload["topics"]]
    assert "ai" in topic_names
    assert "crypto" in topic_names
    assert "climate" in topic_names


async def test_track_topic_write_format(pool, ctx):
    """Verify the DB write uses correct UPSERT SQL."""
    pool.fetchrow = AsyncMock(return_value=None)

    await _exec_track_topic(pool, {"topic_name": "test", "queries": ["q1"]}, ctx)

    call_args = pool.execute.call_args
    sql = call_args[0][0]
    assert "ON CONFLICT" in sql
    assert "intelligence_topics" in sql
    assert "updated_at" in sql
