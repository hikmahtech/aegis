"""ChatActivities.synthesize_reply — thin HTTP wrapper around the core
/api/chat/agent-reply route. Lives in worker so AgentChatReplyFlow can
invoke it via execute_activity_method.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from aegis_worker.activities.chat import ChatActivities
from aegis_worker.activities.core_client import CoreClient


@pytest.mark.asyncio
async def test_synthesize_reply_returns_core_response_verbatim():
    core = CoreClient(base_url="http://core")
    payload = {
        "reply_text": "Hello.",
        "tool_trace_summary": "search_knowledge",
        "llm_model": "claude-sonnet",
        "error": None,
        "error_is_transient": False,
    }
    core.post = AsyncMock(
        return_value=httpx.Response(200, json=payload, request=httpx.Request("POST", "/x"))
    )
    acts = ChatActivities(client=core)

    out = await acts.synthesize_reply(
        agent_id="raphael",
        message="x",
        thread_id="t",
        task_id="abc",
    )

    assert out == payload
    core.post.assert_awaited_once()
    args, kwargs = core.post.call_args
    assert args[0] == "/api/chat/agent-reply"
    assert kwargs["json"] == {
        "agent_id": "raphael",
        "message": "x",
        "thread_id": "t",
        "task_id": "abc",
    }


@pytest.mark.asyncio
async def test_synthesize_reply_raises_on_5xx():
    """Transient → raise so STANDARD retry policy retries the activity."""
    core = CoreClient(base_url="http://core")
    core.post = AsyncMock(
        return_value=httpx.Response(503, json={}, request=httpx.Request("POST", "/x"))
    )
    acts = ChatActivities(client=core)

    with pytest.raises(httpx.HTTPStatusError):
        await acts.synthesize_reply(agent_id="raphael", message="x", thread_id="t", task_id="abc")


def test_core_client_passes_explicit_timeout_through_to_httpx():
    """Pin: CoreClient must honour an explicit timeout kwarg and pass it
    to httpx.AsyncClient. Worker boot constructs CoreClient with a 550s
    timeout for ChatActivities specifically because smart-tier agents
    (pandoras-actor on claude-sonnet) with heavy tools (remote_script
    kimi SSH, deep KS search) legitimately take 3-6 min wall time;
    a regression here surfaces in prod as `ReadTimeout: Application
    error` from synthesize_reply and the AgentChatReplyFlow falls
    into the error-comment compensating path. Caught in prod 2026-05-27.
    """
    c = CoreClient(base_url="http://x", api_key="k", timeout=550)
    # httpx.Timeout: when constructed from a scalar, read/write/connect/pool
    # all take that value. Asserting on .read is sufficient — that's the
    # axis ReadTimeout fires on.
    assert c._client.timeout.read == 550, (
        "CoreClient ignored the explicit timeout — a value too low will "
        "fire spuriously on smart-tier chat calls with heavy tool use"
    )


def test_chat_reply_timeout_matches_chat_path():
    """Pin: TIMEOUT_CHAT_REPLY (used by AgentChatReplyFlow.synthesize_reply)
    must be 600s — matching the chat httpx timeout from PR #248. They
    cover the same workload
    (smart-tier LLM + tool orchestration); divergent values surface as
    one entry point silently dropping replies while the other works.
    """
    from datetime import timedelta

    from aegis_worker.shared.retry import TIMEOUT_CHAT_REPLY

    assert timedelta(seconds=600) == TIMEOUT_CHAT_REPLY
