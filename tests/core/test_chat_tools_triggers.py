"""Tests for chat workflow trigger tools."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aegis.services.chat import ToolContext, _execute_tool
from aegis.services.workflows import TRIGGERABLE_WORKFLOWS, trigger_workflow


@pytest.fixture
def ctx():
    return ToolContext()


async def test_trigger_workflow_valid():
    client = AsyncMock()
    handle = MagicMock()
    handle.id = "chat-daily_briefing-abc123"
    client.start_workflow.return_value = handle

    result = await trigger_workflow(client, "daily_briefing")
    assert result["status"] == "started"
    assert "daily_briefing" in result["workflow_id"]
    client.start_workflow.assert_called_once()


async def test_trigger_workflow_unknown_type():
    client = AsyncMock()
    result = await trigger_workflow(client, "nonexistent")
    assert "error" in result


async def test_trigger_workflow_chat_tool(ctx):
    pool = AsyncMock()
    client = AsyncMock()
    handle = MagicMock()
    handle.id = "chat-daily_briefing-xyz"
    client.start_workflow.return_value = handle
    ctx.temporal_client = client

    result = await _execute_tool(pool, "trigger_workflow", {"workflow_type": "daily_briefing"}, ctx)
    data = json.loads(result)
    assert data["status"] == "started"


async def test_trigger_workflow_no_temporal_client(ctx):
    pool = AsyncMock()
    ctx.temporal_client = None
    result = await _execute_tool(pool, "trigger_workflow", {"workflow_type": "daily_briefing"}, ctx)
    data = json.loads(result)
    assert "error" in data


def test_triggerable_workflows_defined():
    assert "daily_briefing" in TRIGGERABLE_WORKFLOWS
