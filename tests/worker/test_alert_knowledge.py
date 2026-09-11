"""Tests for AlertActivities.gather_alert_knowledge.

The recall itself — ranking by outcome, leaving discarded fixes out — runs on
the real knowledge store in `test_alert_verdict_kg.py`. These pin the edges
with a stand-in store whose `search` returns what `KnowledgeStore.search`
returns.
"""

from unittest.mock import AsyncMock

import pytest
from aegis_worker.activities.alerts import AlertActivities
from temporalio.testing import ActivityEnvironment


def _hit(title: str, content: str, outcome: str = "", similarity: float = 0.8) -> dict:
    """One row as `KnowledgeStore.search` shapes it."""
    return {
        "content_id": title,
        "id": title,
        "title": title,
        "url": f"aegis://alert/{title}" + (f"#{outcome}" if outcome else ""),
        "source_type": "alert_investigation",
        "tags": ["alert"],
        "metadata": {"status": "actionable", **({"outcome": outcome} if outcome else {})},
        "summary": None,
        "content": content,
        "similarity": similarity,
        "ingested_at": "2026-09-10T08:00:00+00:00",
        "created_at": "2026-09-10T08:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_gather_alert_knowledge_returns_prior_incidents():
    env = ActivityEnvironment()
    mock_kc = AsyncMock()
    mock_kc.search.return_value = [
        _hit("Alert investigation: NullPointerException in auth", "Seen in the auth service last week.")
    ]
    act = AlertActivities(db_pool=None, llm_client=None, knowledge_connector=mock_kc)
    result = await env.run(act.gather_alert_knowledge, "NullPointerException in auth", "aegis")
    assert "auth service" in result
    assert "2026-09-10" in result
    # Only past verdicts are searched, not the whole corpus.
    assert mock_kc.search.await_args.kwargs["source_type"] == "alert_investigation"
    mock_kc.ask.assert_not_awaited()


@pytest.mark.asyncio
async def test_gather_alert_knowledge_no_connector():
    env = ActivityEnvironment()
    act = AlertActivities(db_pool=None, llm_client=None, knowledge_connector=None)
    result = await env.run(act.gather_alert_knowledge, "Error", "test")
    assert result == ""


@pytest.mark.asyncio
async def test_gather_alert_knowledge_error_returns_empty():
    env = ActivityEnvironment()
    mock_kc = AsyncMock()
    mock_kc.search.side_effect = Exception("timeout")
    act = AlertActivities(db_pool=None, llm_client=None, knowledge_connector=mock_kc)
    result = await env.run(act.gather_alert_knowledge, "Error", "test")
    assert result == ""


@pytest.mark.asyncio
async def test_at_most_three_prior_incidents_are_shown():
    env = ActivityEnvironment()
    mock_kc = AsyncMock()
    mock_kc.search.return_value = [_hit(f"t{i}", f"verdict {i}") for i in range(6)]
    act = AlertActivities(db_pool=None, llm_client=None, knowledge_connector=mock_kc)
    result = await env.run(act.gather_alert_knowledge, "Error", "")
    assert [f"verdict {i}" in result for i in range(6)] == [True, True, True, False, False, False]
