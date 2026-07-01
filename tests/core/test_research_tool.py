"""Tests for research_topic chat tool."""

import json
from unittest.mock import AsyncMock

import pytest
from aegis.services.chat import ToolContext, _exec_research_topic


@pytest.fixture
def pool():
    return AsyncMock()


@pytest.fixture
def ctx_with_connectors():
    ctx = ToolContext(
        agent_id="sebas",
        knowledge_connector=AsyncMock(
            search=AsyncMock(
                return_value=[
                    {
                        "content": "Previous AI research",
                        "similarity": 0.7,
                        "title": "AI Overview",
                        "summary": "Prior KG data",
                    }
                ]
            ),
            ingest_content=AsyncMock(return_value={"ok": True}),
        ),
        search_connector=AsyncMock(
            search=AsyncMock(
                return_value=[
                    {
                        "title": "New AI Model",
                        "url": "https://example.com/ai",
                        "content": "A new model released...",
                    },
                ]
            ),
        ),
        llm_client=AsyncMock(),
    )
    ctx.llm_client.think = AsyncMock(return_value={"response": "AI is advancing rapidly."})
    return ctx


async def test_research_topic_quick(pool, ctx_with_connectors):
    result = await _exec_research_topic(
        pool, {"query": "latest AI", "depth": "quick"}, ctx_with_connectors
    )
    data = json.loads(result)
    assert "synthesis" in data
    assert data["synthesis"] == "AI is advancing rapidly."
    assert data["sources"]["knowledge_graph"] == 1
    assert data["sources"]["web_search"] == 1
    assert len(data["top_urls"]) == 1
    assert data["top_urls"][0] == "https://example.com/ai"


async def test_research_topic_thorough_uses_higher_limit(pool):
    search_connector = AsyncMock()
    search_connector.search = AsyncMock(
        return_value=[
            {"title": f"Result {i}", "url": f"https://example.com/{i}", "content": "text"}
            for i in range(5)
        ]
    )
    ctx = ToolContext(
        agent_id="raphael",
        search_connector=search_connector,
        llm_client=AsyncMock(),
    )
    ctx.llm_client.think = AsyncMock(return_value={"response": "Thorough synthesis."})

    result = await _exec_research_topic(pool, {"query": "test", "depth": "thorough"}, ctx)
    data = json.loads(result)
    assert "synthesis" in data

    # Verify limit=20 was passed for thorough depth
    call_kwargs = search_connector.search.call_args
    assert call_kwargs[1].get("limit") == 20 or call_kwargs[0][1] == 20


async def test_research_topic_quick_uses_limit_10(pool):
    search_connector = AsyncMock()
    search_connector.search = AsyncMock(return_value=[])
    ctx = ToolContext(
        agent_id="sebas",
        search_connector=search_connector,
        llm_client=AsyncMock(),
    )
    ctx.llm_client.think = AsyncMock(return_value={"response": "No results."})

    await _exec_research_topic(pool, {"query": "test", "depth": "quick"}, ctx)
    call_kwargs = search_connector.search.call_args
    limit_arg = call_kwargs[1].get("limit") if call_kwargs[1] else call_kwargs[0][1]
    assert limit_arg == 10


async def test_research_topic_domain_filter(pool):
    search_connector = AsyncMock()
    search_connector.search = AsyncMock(
        return_value=[
            {"title": "arxiv paper", "url": "https://arxiv.org/paper1", "content": "ML research"}
        ]
    )
    ctx = ToolContext(
        agent_id="raphael",
        search_connector=search_connector,
        llm_client=AsyncMock(),
    )
    ctx.llm_client.think = AsyncMock(return_value={"response": "Domain-filtered research."})

    result = await _exec_research_topic(
        pool,
        {"query": "machine learning", "domains": ["arxiv.org", "scholar.google.com"]},
        ctx,
    )
    data = json.loads(result)
    assert "synthesis" in data

    # Verify domain terms were included in the query
    call_args = search_connector.search.call_args
    query_arg = call_args[0][0] if call_args[0] else call_args[1].get("query", "")
    assert "site:arxiv.org" in query_arg or "arxiv.org" in query_arg


async def test_research_topic_no_connectors(pool):
    ctx = ToolContext(agent_id="sebas")  # no connectors
    result = await _exec_research_topic(pool, {"query": "test"}, ctx)
    data = json.loads(result)
    assert "error" in data


async def test_research_topic_no_search_connector(pool):
    ctx = ToolContext(
        agent_id="sebas",
        knowledge_connector=AsyncMock(),
        llm_client=AsyncMock(),
        # search_connector intentionally omitted
    )
    result = await _exec_research_topic(pool, {"query": "test"}, ctx)
    data = json.loads(result)
    assert "error" in data


async def test_research_topic_no_llm_client(pool):
    ctx = ToolContext(
        agent_id="sebas",
        search_connector=AsyncMock(),
        # llm_client intentionally omitted
    )
    result = await _exec_research_topic(pool, {"query": "test"}, ctx)
    data = json.loads(result)
    assert "error" in data


async def test_research_topic_no_kg_results(pool):
    """Should still work when KG returns no results."""
    search_connector = AsyncMock()
    search_connector.search = AsyncMock(
        return_value=[
            {"title": "Web only", "url": "https://example.com", "content": "some content"}
        ]
    )
    ctx = ToolContext(
        agent_id="sebas",
        search_connector=search_connector,
        # No knowledge_connector
        llm_client=AsyncMock(),
    )
    ctx.llm_client.think = AsyncMock(return_value={"response": "Web-only synthesis."})

    result = await _exec_research_topic(pool, {"query": "test"}, ctx)
    data = json.loads(result)
    assert "synthesis" in data
    assert data["sources"]["knowledge_graph"] == 0
    assert data["sources"]["web_search"] == 1


async def test_research_topic_no_results_at_all(pool):
    """Returns graceful response when both KG and web return empty."""
    search_connector = AsyncMock()
    search_connector.search = AsyncMock(return_value=[])
    knowledge_connector = AsyncMock()
    knowledge_connector.search = AsyncMock(return_value=[])
    ctx = ToolContext(
        agent_id="sebas",
        search_connector=search_connector,
        knowledge_connector=knowledge_connector,
        llm_client=AsyncMock(),
    )

    result = await _exec_research_topic(pool, {"query": "obscure_topic_xyz"}, ctx)
    data = json.loads(result)
    assert "synthesis" in data
    assert data["sources"]["knowledge_graph"] == 0
    assert data["sources"]["web_search"] == 0


async def test_research_topic_top_urls_capped_at_5(pool):
    """top_urls in response should be capped at 5."""
    web_results = [
        {"title": f"Result {i}", "url": f"https://example.com/{i}", "content": "text"}
        for i in range(10)
    ]
    search_connector = AsyncMock()
    search_connector.search = AsyncMock(return_value=web_results)
    ctx = ToolContext(
        agent_id="sebas",
        search_connector=search_connector,
        llm_client=AsyncMock(),
    )
    ctx.llm_client.think = AsyncMock(return_value={"response": "Many results."})

    result = await _exec_research_topic(pool, {"query": "popular topic"}, ctx)
    data = json.loads(result)
    assert len(data["top_urls"]) <= 5


async def test_research_topic_kg_error_graceful(pool):
    """KG failure should not crash the tool — falls back to web-only."""
    knowledge_connector = AsyncMock()
    knowledge_connector.search = AsyncMock(side_effect=Exception("KG down"))
    search_connector = AsyncMock()
    search_connector.search = AsyncMock(
        return_value=[{"title": "Web result", "url": "https://example.com", "content": "content"}]
    )
    ctx = ToolContext(
        agent_id="sebas",
        knowledge_connector=knowledge_connector,
        search_connector=search_connector,
        llm_client=AsyncMock(),
    )
    ctx.llm_client.think = AsyncMock(return_value={"response": "Web fallback synthesis."})

    result = await _exec_research_topic(pool, {"query": "test"}, ctx)
    data = json.loads(result)
    assert "synthesis" in data
    assert data["sources"]["knowledge_graph"] == 0
    assert data["sources"]["web_search"] == 1
