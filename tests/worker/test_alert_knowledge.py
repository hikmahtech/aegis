"""Tests for AlertActivities.gather_alert_knowledge."""

from unittest.mock import AsyncMock

import pytest
from aegis_worker.activities.alerts import AlertActivities
from temporalio.testing import ActivityEnvironment


@pytest.mark.asyncio
async def test_gather_alert_knowledge_returns_answer():
    env = ActivityEnvironment()
    mock_kc = AsyncMock()
    mock_kc.ask.return_value = {
        "answer": "This error was seen before in the auth service last week.",
        "confidence": 0.8,
        "sources": [],
    }
    act = AlertActivities(
        db_pool=None,
        llm_client=None,
        knowledge_connector=mock_kc,
    )
    result = await env.run(act.gather_alert_knowledge, "NullPointerException in auth", "aegis")
    assert "auth service" in result


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
    mock_kc.ask.side_effect = Exception("timeout")
    act = AlertActivities(db_pool=None, llm_client=None, knowledge_connector=mock_kc)
    result = await env.run(act.gather_alert_knowledge, "Error", "test")
    assert result == ""
