"""Tests for improved proactive knowledge context."""

from unittest.mock import AsyncMock

from aegis.services.chat import _extract_query_entities, _gather_knowledge_context


def test_extract_entities_capitalized():
    entities = _extract_query_entities("What happened with Daily Briefing?")
    assert "Daily Briefing" in entities


def test_extract_entities_quoted():
    entities = _extract_query_entities('Tell me about "Homelab GitOps"')
    assert "Homelab GitOps" in entities


def test_extract_entities_agent_names():
    entities = _extract_query_entities("What did sebas do today?")
    assert "sebas" in entities


def test_extract_entities_empty():
    entities = _extract_query_entities("hi")
    assert entities == []


def test_extract_entities_max_two():
    entities = _extract_query_entities("Ask Sebas about Daily Briefing and Homelab GitOps")
    assert len(entities) <= 2


async def test_gather_context_merges_search_and_kg():
    kc = AsyncMock()
    kc.search.return_value = [
        {
            "title": "Alert A",
            "similarity": 0.8,
            "source_type": "alert",
            "summary": "Server down",
            "url": "aegis://alert/1",
        },
    ]
    kc.query_kg.return_value = [
        {
            "subject": "Daily Briefing",
            "predicate": "mentioned",
            "object": "Server down",
            "confidence": 0.9,
        },
    ]
    context, injected = await _gather_knowledge_context(
        kc, "What happened with Daily Briefing?", agent_id="sebas"
    )
    assert context is not None
    assert "Alert A" in context or "Daily Briefing" in context
    assert len(injected) > 0
    assert "content_hash" in injected[0]
    assert "keywords" in injected[0]


async def test_gather_context_boosts_agent_domains():
    kc = AsyncMock()
    kc.search.return_value = [
        {
            "title": "Sentry Issue",
            "similarity": 0.7,
            "source_type": "sentry",
            "summary": "Error 500",
            "url": "a",
        },
        {
            "title": "Task Done",
            "similarity": 0.7,
            "source_type": "task_outcome",
            "summary": "Fixed",
            "url": "b",
        },
    ]
    kc.query_kg.return_value = []
    context, injected = await _gather_knowledge_context(
        kc, "what broke?", agent_id="pandoras-actor"
    )
    assert context is not None
    assert "Sentry Issue" in context
    assert len(injected) > 0


async def test_gather_context_handles_kg_failure():
    kc = AsyncMock()
    kc.search.return_value = [
        {
            "title": "Test",
            "similarity": 0.8,
            "source_type": "article",
            "summary": "Content",
            "url": "x",
        },
    ]
    kc.query_kg.side_effect = Exception("KG down")
    context, injected = await _gather_knowledge_context(
        kc, "query about Daily Briefing", agent_id="sebas"
    )
    assert context is not None
    assert "Test" in context
    assert len(injected) > 0


async def test_gather_context_no_connector():
    context, injected = await _gather_knowledge_context(None, "test message")
    assert context is None
    assert injected == []


async def test_gather_context_handles_null_similarity():
    """knowledge-service returns similarity=null for BM25-only chunks.

    Regression: the boost/sort/decay paths used `r.get("similarity", 0)` which
    only handles a *missing* key — a present-but-null value flowed through as
    None and crashed `None + float`, killing the whole inject path silently
    (TRY/EXCEPT logged a warning and returned (None, []) every time).
    """
    kc = AsyncMock()
    kc.search.return_value = [
        {
            "title": "BM25-only hit",
            "similarity": None,
            "source_type": "runbook",
            "summary": "Restart procedure",
            "url": "ks://content/bm25",
            "content_id": "bm25-1",
        },
    ]
    kc.query_kg.return_value = []
    context, injected = await _gather_knowledge_context(
        kc, "restart procedure", agent_id="raphael", score_threshold=-1.0
    )
    assert context is not None
    assert "BM25-only hit" in context
    assert len(injected) == 1
    assert injected[0]["content_id"] == "bm25-1"


async def test_gather_context_below_threshold():
    kc = AsyncMock()
    kc.search.return_value = [
        {
            "title": "Low",
            "similarity": 0.1,
            "source_type": "article",
            "summary": "Irrelevant",
            "url": "x",
        },
    ]
    kc.query_kg.return_value = []
    context, injected = await _gather_knowledge_context(kc, "random query", agent_id="sebas")
    assert context is None
    assert injected == []


# NOTE: the old HTTP-param-forwarding tests for KnowledgeConnector were removed
# with that connector — the native pgvector KnowledgeStore filters content_id in
# SQL, covered by tests/core/test_knowledge_store.py.
