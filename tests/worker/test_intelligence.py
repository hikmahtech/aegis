"""Tests for intelligence activities."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aegis_worker.activities.intelligence import IntelligenceActivities
from temporalio.testing import ActivityEnvironment


@pytest.fixture
def mock_kc():
    kc = AsyncMock()
    kc.search = AsyncMock(return_value=[])
    kc.ingest_claims = AsyncMock(return_value={"status": "ok"})
    kc.ingest_content = AsyncMock(return_value={"status": "ok"})
    return kc


async def test_dedup_items_passes_novel_items(mock_kc):
    mock_kc.search = AsyncMock(return_value=[])
    act = IntelligenceActivities(knowledge_connector=mock_kc)
    env = ActivityEnvironment()
    items = [
        {
            "title": "New AI breakthrough",
            "url": "https://example.com/ai",
            "snippet": "A new model...",
        },
        {
            "title": "BRICS expansion",
            "url": "https://example.com/brics",
            "snippet": "New members...",
        },
    ]
    result = await env.run(act.dedup_items, items)
    assert len(result) == 2


async def test_dedup_items_removes_duplicates(mock_kc):
    mock_kc.search = AsyncMock(return_value=[{"similarity": 0.9, "content": "Already seen"}])
    act = IntelligenceActivities(knowledge_connector=mock_kc)
    env = ActivityEnvironment()
    items = [{"title": "Old news", "url": "https://example.com/old", "snippet": "..."}]
    result = await env.run(act.dedup_items, items)
    assert len(result) == 0


async def test_score_significance(mock_kc):
    llm = MagicMock()
    llm.think = AsyncMock(
        return_value={
            "response": json.dumps(
                [
                    {"index": 0, "score": 4, "reason": "Major AI development"},
                    {"index": 1, "score": 2, "reason": "Minor financial news"},
                ]
            ),
            "model": "gemma4:e2b",
            "prompt_tokens": 10,
            "completion_tokens": 20,
        }
    )
    act = IntelligenceActivities(knowledge_connector=mock_kc, llm_client=llm)
    env = ActivityEnvironment()
    items = [
        {"title": "GPT-5 released", "snippet": "OpenAI announces..."},
        {"title": "Minor stock dip", "snippet": "S&P down 0.1%"},
    ]
    topics = [{"name": "ai", "queries": ["AI", "LLM"], "priority": "high"}]
    result = await env.run(act.score_significance, items, topics)
    assert len(result) == 2
    assert result[0]["significance"] == 4
    assert result[1]["significance"] == 2


async def test_score_significance_no_llm(mock_kc):
    act = IntelligenceActivities(knowledge_connector=mock_kc)
    env = ActivityEnvironment()
    items = [{"title": "Test", "snippet": "..."}]
    result = await env.run(act.score_significance, items, [])
    assert result[0]["significance"] == 3


async def test_score_significance_uses_light_model_with_headroom(mock_kc):
    """Significance scoring runs on the fast tier (gemma4:e2b), not the balanced
    tier — gpt-oss:20b intermittently hangs >180s under proxy load (PR #319).
    gemma4 returns empty below ~900 tokens, so a generous max_tokens is required
    for it to emit the scored JSON array at all (validated live: 4/4 at mt=900)."""
    captured: dict = {}

    async def fake_think(**kwargs):
        captured.update(kwargs)
        return {"response": "[]", "model": kwargs.get("model")}

    llm = MagicMock()
    llm.think = fake_think
    act = IntelligenceActivities(knowledge_connector=mock_kc, llm_client=llm)
    env = ActivityEnvironment()
    await env.run(act.score_significance, [{"title": "x", "snippet": "y"}], [{"name": "ai"}])
    assert captured["model"] == "gemma4:e2b"  # fast tier, not the balanced gpt-oss:20b
    assert captured["max_tokens"] >= 900  # headroom so gemma4 doesn't return empty


async def test_ingest_intelligence(mock_kc):
    act = IntelligenceActivities(knowledge_connector=mock_kc)
    env = ActivityEnvironment()
    analyses = [
        {
            "title": "GPT-5",
            "summary": "Major release",
            "claims": [{"subject": "GPT-5", "predicate": "released_by", "object": "OpenAI"}],
            "topic": "ai",
            "url": "https://example.com",
        },
    ]
    result = await env.run(act.ingest_intelligence, analyses)
    assert result["ingested"] == 1
    # intel items are captured as content chunks, not graph claims
    mock_kc.ingest_content.assert_called_once()
    mock_kc.ingest_claims.assert_not_called()


async def test_ingest_intelligence_snippet_shape(mock_kc):
    """Regression: IntelligenceScanFlow items carry `snippet` (from
    intel_scan.search_source), NOT `summary`. Gating on `summary` alone
    silently ingested 0 worthy items into KS for weeks. The content gate
    must fall back to snippet."""
    act = IntelligenceActivities(knowledge_connector=mock_kc)
    env = ActivityEnvironment()
    analyses = [
        {
            "title": "UN climate report",
            "snippet": "The window is closing rapidly...",
            "url": "https://example.com/climate",
            "significance": 7,
        },
    ]
    result = await env.run(act.ingest_intelligence, analyses)
    assert result["ingested"] == 1, "snippet-shaped item must ingest into KS"
    mock_kc.ingest_content.assert_called_once()
    _, kwargs = mock_kc.ingest_content.call_args
    assert kwargs["summary"] == "The window is closing rapidly..."
    assert kwargs["source_type"] == "intelligence"
