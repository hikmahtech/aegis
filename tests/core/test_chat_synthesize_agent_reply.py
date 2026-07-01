"""synthesize_agent_reply: non-Telegram chat entry point used by
AgentChatReplyFlow (Todoist comment channel).

Contract (per spec):
  - On success: returns {reply_text, tool_trace_summary, llm_model,
    error: None, error_is_transient: False}.
  - On agent-not-found OR LLM refusal/empty: returns reply_text="",
    error="...", error_is_transient=False. NEVER raises.
  - On LLM-proxy 5xx / connect / timeout: RAISES so the calling route
    returns 5xx and the worker activity retries via STANDARD policy.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from aegis.services.chat import synthesize_agent_reply


@pytest.mark.asyncio
async def test_synthesize_agent_reply_happy_path(mock_db_pool):
    """Happy path: send_message returns a response → contract fields filled."""
    fake_response = {
        "response": "Tigris is an S3-compatible store...",
        "agent_id": "raphael",
        "tool_calls": [{"name": "search_knowledge", "args": {"q": "Tigris"}}],
        "model": "claude-sonnet",
    }
    with patch("aegis.services.chat.send_message", new=AsyncMock(return_value=fake_response)):
        out = await synthesize_agent_reply(
            pool=mock_db_pool,
            llm_client=AsyncMock(),
            agent_id="raphael",
            message="user comment...",
            thread_id="todoist-task-abc",
            task_id="abc",
        )

    assert out["reply_text"] == "Tigris is an S3-compatible store..."
    assert out["tool_trace_summary"] == "search_knowledge"
    assert out["llm_model"] == "claude-sonnet"
    assert out["error"] is None
    assert out["error_is_transient"] is False


@pytest.mark.asyncio
async def test_synthesize_agent_reply_agent_not_found(mock_db_pool):
    """Agent missing → send_message returns {error}; we return error,
    no raise (route returns 200)."""
    with patch(
        "aegis.services.chat.send_message",
        new=AsyncMock(return_value={"error": "Agent 'foo' not found", "response": ""}),
    ):
        out = await synthesize_agent_reply(
            pool=mock_db_pool,
            llm_client=AsyncMock(),
            agent_id="foo",
            message="x",
            thread_id="t",
            task_id="abc",
        )

    assert out["reply_text"] == ""
    assert "not found" in out["error"]
    assert out["error_is_transient"] is False


@pytest.mark.asyncio
async def test_synthesize_agent_reply_transient_5xx_raises(mock_db_pool):
    """LLM proxy 5xx → send_message raises httpx.HTTPStatusError → we
    re-raise (route returns 5xx → activity retries)."""
    import httpx

    raise_exc = httpx.HTTPStatusError(
        "503",
        request=httpx.Request("POST", "https://x"),
        response=httpx.Response(503, request=httpx.Request("POST", "https://x")),
    )
    with (
        patch("aegis.services.chat.send_message", new=AsyncMock(side_effect=raise_exc)),
        pytest.raises(httpx.HTTPStatusError),
    ):
        await synthesize_agent_reply(
            pool=mock_db_pool,
            llm_client=AsyncMock(),
            agent_id="raphael",
            message="x",
            thread_id="t",
            task_id="abc",
        )


@pytest.mark.asyncio
async def test_synthesize_agent_reply_passes_metadata(mock_db_pool):
    """metadata={surface, task_id} threads through to send_message."""
    send_mock = AsyncMock(return_value={"response": "ok", "model": "c"})
    with patch("aegis.services.chat.send_message", new=send_mock):
        await synthesize_agent_reply(
            pool=mock_db_pool,
            llm_client=AsyncMock(),
            agent_id="raphael",
            message="x",
            thread_id="t",
            task_id="abc",
        )
    kwargs = send_mock.call_args.kwargs
    assert kwargs.get("user_metadata", {}).get("surface") == "todoist_comment"
    assert kwargs.get("user_metadata", {}).get("task_id") == "abc"


@pytest.mark.asyncio
async def test_synthesize_agent_reply_threads_llm_client_to_send_message(mock_db_pool):
    """llm_client passed to synthesize_agent_reply must reach send_message —
    without this, production calls fail with TypeError (caught by final
    review on PR #261).
    """
    fake_llm = AsyncMock()
    send_mock = AsyncMock(return_value={"response": "ok", "model": "c"})
    with patch("aegis.services.chat.send_message", new=send_mock):
        await synthesize_agent_reply(
            pool=mock_db_pool,
            llm_client=fake_llm,
            agent_id="raphael",
            message="x",
            thread_id="t",
            task_id="abc",
        )
    assert send_mock.call_args.kwargs.get("llm_client") is fake_llm


@pytest.mark.asyncio
async def test_synthesize_agent_reply_forwards_temporal_client(mock_db_pool):
    """Regression: temporal_client must reach send_message (and thus the
    tool ToolContext) — otherwise tools like investigate_resource that spawn
    workflows get ctx.temporal_client=None and fail with 'temporal client
    not available'. The comment-channel path previously dropped it."""
    sentinel = object()
    send = AsyncMock(return_value={"response": "ok", "tool_calls": [], "model": "m"})
    with patch("aegis.services.chat.send_message", new=send):
        await synthesize_agent_reply(
            pool=mock_db_pool,
            llm_client=AsyncMock(),
            agent_id="pandoras-actor",
            message="investigate this in bcp",
            thread_id="t",
            task_id="abc",
            temporal_client=sentinel,
        )
    assert send.await_args.kwargs["temporal_client"] is sentinel
