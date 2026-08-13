"""dispatch_agent_run — the chat-side door to the heavy lane (AgentRunFlow).

Core never imports worker code, so the only contract to lock is the one that
crosses the boundary: the workflow NAME, the task queue, and the arg dict the
flow's dataclass is built from.
"""

from unittest.mock import AsyncMock, MagicMock

from aegis.services.chat import CHAT_TOOLS, TOOL_EXECUTORS, ToolContext, _execute_tool


def _client() -> AsyncMock:
    client = AsyncMock()
    handle = MagicMock()
    handle.id = "agent-run-deadbeef"
    client.start_workflow.return_value = handle
    return client


async def test_dispatch_starts_agent_run_flow_with_agent_and_prompt():
    pool = AsyncMock()
    client = _client()
    ctx = ToolContext(agent_id="pandoras-actor", temporal_client=client)

    result = await _execute_tool(
        pool,
        "dispatch_agent_run",
        {
            "prompt": "Trace why gmail-ingest drops attachments.",
            "repo": "aegis",
            "engine": "claude",
            "purpose": "gmail attachment trace",
        },
        ctx,
    )

    client.start_workflow.assert_called_once()
    args, kwargs = client.start_workflow.call_args
    assert args[0] == "AgentRunFlow"
    payload = args[1]
    # agent_id comes from the calling agent's context, never from the model.
    assert payload["agent_id"] == "pandoras-actor"
    assert payload["prompt"] == "Trace why gmail-ingest drops attachments."
    assert payload["repo"] == "aegis"
    assert payload["engine"] == "claude"
    assert payload["purpose"] == "gmail attachment trace"
    assert kwargs["task_queue"] == "aegis-main"
    assert kwargs["id"].startswith("agent-run-")

    assert kwargs["id"] in result
    assert "results will land in this channel" in result


async def test_dispatch_omits_repo_as_none_so_the_flow_picks_scratch():
    pool = AsyncMock()
    client = _client()
    ctx = ToolContext(agent_id="sebas", temporal_client=client)

    await _execute_tool(pool, "dispatch_agent_run", {"prompt": "research X"}, ctx)

    payload = client.start_workflow.call_args[0][1]
    assert payload["repo"] is None
    assert payload["engine"] == ""


async def test_dispatch_without_temporal_client_returns_error_text():
    pool = AsyncMock()
    ctx = ToolContext(agent_id="sebas", temporal_client=None)
    result = await _execute_tool(pool, "dispatch_agent_run", {"prompt": "x"}, ctx)
    assert "Temporal client not available" in result


async def test_dispatch_rejects_unknown_engine_without_starting_anything():
    pool = AsyncMock()
    client = _client()
    ctx = ToolContext(agent_id="sebas", temporal_client=client)
    result = await _execute_tool(
        pool, "dispatch_agent_run", {"prompt": "x", "engine": "gpt"}, ctx
    )
    assert "unknown engine" in result
    client.start_workflow.assert_not_called()


async def test_dispatch_requires_a_prompt():
    pool = AsyncMock()
    client = _client()
    ctx = ToolContext(agent_id="sebas", temporal_client=client)
    result = await _execute_tool(pool, "dispatch_agent_run", {"prompt": "   "}, ctx)
    assert "prompt is required" in result
    client.start_workflow.assert_not_called()


async def test_dispatch_failure_is_reported_not_raised():
    """Temporal being down must come back as a chat answer, not blow up the
    tool loop mid-reply."""
    pool = AsyncMock()
    client = AsyncMock()
    client.start_workflow.side_effect = RuntimeError("temporal frontend unreachable")
    ctx = ToolContext(agent_id="sebas", temporal_client=client)

    result = await _execute_tool(pool, "dispatch_agent_run", {"prompt": "x"}, ctx)
    assert "temporal frontend unreachable" in result


def test_tool_is_advertised_and_dispatchable():
    """A schema with no executor is never dispatched; an executor with no
    schema is never advertised. Both halves have to exist."""
    schemas = [t for t in CHAT_TOOLS if t["function"]["name"] == "dispatch_agent_run"]
    assert len(schemas) == 1
    assert "dispatch_agent_run" in TOOL_EXECUTORS
    params = schemas[0]["function"]["parameters"]
    assert params["required"] == ["prompt"]
    assert params["properties"]["engine"]["enum"] == ["claude", "kimi"]
