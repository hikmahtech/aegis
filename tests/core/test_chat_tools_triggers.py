"""Tests for chat workflow trigger tools."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aegis.services.chat import ToolContext, _execute_tool
from aegis.services.workflows import trigger_workflow


@pytest.fixture
def ctx():
    return ToolContext()


def _pool_with_types(*workflow_types):
    pool = AsyncMock()
    pool.fetch.return_value = [{"workflow_type": t} for t in workflow_types]
    return pool


async def test_trigger_workflow_valid():
    pool = _pool_with_types("DailyBriefingFlow", "WeeklyReviewFlow")
    client = AsyncMock()
    handle = MagicMock()
    handle.id = "chat-DailyBriefingFlow-abc123"
    client.start_workflow.return_value = handle

    result = await trigger_workflow(client, pool, "DailyBriefingFlow")
    assert result["status"] == "started"
    assert "DailyBriefingFlow" in result["workflow_id"]
    client.start_workflow.assert_called_once()


async def test_trigger_workflow_previously_untriggerable_flow_now_valid():
    """ClarifyFlow wasn't in the old hardcoded 2-entry TRIGGERABLE_WORKFLOWS dict;
    the DB-backed lookup over activities.workflow_type must accept it now since
    it's a real scheduled flow (config/seed/activities.yaml: gtd-clarify-15min)."""
    pool = _pool_with_types("ClarifyFlow")
    client = AsyncMock()
    handle = MagicMock()
    handle.id = "chat-ClarifyFlow-xyz"
    client.start_workflow.return_value = handle

    result = await trigger_workflow(client, pool, "ClarifyFlow")
    assert result["status"] == "started"


async def test_trigger_workflow_unknown_type():
    pool = _pool_with_types("DailyBriefingFlow")
    client = AsyncMock()
    result = await trigger_workflow(client, pool, "nonexistent")
    assert "error" in result
    assert "nonexistent" in result["error"]


async def test_trigger_workflow_chat_tool(ctx):
    pool = _pool_with_types("DailyBriefingFlow")
    client = AsyncMock()
    handle = MagicMock()
    handle.id = "chat-DailyBriefingFlow-xyz"
    client.start_workflow.return_value = handle
    ctx.temporal_client = client

    result = await _execute_tool(
        pool, "trigger_workflow", {"workflow_type": "DailyBriefingFlow"}, ctx
    )
    data = json.loads(result)
    assert data["status"] == "started"


async def test_trigger_workflow_no_temporal_client(ctx):
    pool = AsyncMock()
    ctx.temporal_client = None
    result = await _execute_tool(pool, "trigger_workflow", {"workflow_type": "DailyBriefingFlow"}, ctx)
    data = json.loads(result)
    assert "error" in data
