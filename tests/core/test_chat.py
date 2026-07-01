"""Tests for chat endpoint."""

import base64
from unittest.mock import AsyncMock

import pytest
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def mock_llm():
    """Mock LLM client with chat() method for tool calling."""
    llm = AsyncMock()
    # chat() returns response without tool calls (simple response)
    llm.chat = AsyncMock(
        return_value={
            "response": "Hello! I'm Sebas. Let me check your tasks.",
            "tool_calls": [],
            "model": "kimi-k2.5",
            "prompt_tokens": 10,
            "completion_tokens": 20,
        }
    )
    llm.close = AsyncMock()
    return llm


@pytest.fixture
def app(test_settings, mock_db_pool, mock_llm):
    application = create_app(run_lifespan=False)
    application.dependency_overrides[get_settings] = lambda: test_settings
    application.state.db_pool = mock_db_pool
    application.state.llm = mock_llm
    return application


@pytest.fixture
def auth_headers():
    creds = base64.b64encode(b"admin:admin").decode()
    return {"Authorization": f"Basic {creds}"}


async def test_chat_sends_message(app, auth_headers, mock_db_pool):
    """Chat endpoint sends message to agent and returns response."""
    # v3 agents shape: `system_prompt_path` file reference, no `system_prompt` column.
    mock_db_pool.fetchrow.return_value = {
        "id": "sebas",
        "name": "Sebas",
        "system_prompt_path": "personalities/sebas/SOUL.md",
        "role": "executive-assistant",
    }
    mock_db_pool.fetch.return_value = []  # No history

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/chat",
            headers=auth_headers,
            json={
                "agent_id": "sebas",
                "message": "What are my tasks today?",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_id"] == "sebas"
        assert "Sebas" in data["response"]


async def test_chat_missing_fields(app, auth_headers):
    """Chat without required fields returns 400."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/chat", headers=auth_headers, json={"agent_id": "sebas"})
        assert resp.status_code == 400


async def test_chat_surfaces_dispatch_rows_as_assistant_turns(
    app, auth_headers, mock_db_pool, mock_llm
):
    """role='dispatch' rows are messages the user saw on Telegram (briefings,
    interaction cards, alert verdicts). The chat loader must surface them
    to the LLM as assistant turns with a "[Sent to you on Telegram]"
    prefix so the model can reason about what the user is referring to
    even when the reference wasn't part of the conversation proper.
    """
    mock_db_pool.fetchrow.return_value = {
        "id": "pandoras-actor",
        "name": "Pandora",
        "system_prompt_path": "personalities/pandoras-actor",
        "role": "infrastructure",
    }
    mock_db_pool.fetch.return_value = [
        {"role": "dispatch", "content": "Morning briefing: 3 alerts, 1 PR open"},
        {"role": "user", "content": "tell me about the alerts"},
        {"role": "assistant", "content": "There are three: A, B, C."},
    ][::-1]  # SELECT returns DESC; loader reverses

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/chat",
            headers=auth_headers,
            json={
                "agent_id": "pandoras-actor",
                "thread_id": "2755",
                "message": "what was that briefing again?",
            },
        )
        assert resp.status_code == 200

    messages = mock_llm.chat.call_args.kwargs["messages"]
    # message[0] is system. The history should include the dispatch as an
    # assistant turn with the [Sent to you on Telegram] prefix.
    assistant_contents = [m["content"] for m in messages if m["role"] == "assistant"]
    dispatched = [c for c in assistant_contents if "[Sent to you on Telegram]" in c]
    assert dispatched, f"dispatch never surfaced to LLM: {assistant_contents!r}"
    assert "Morning briefing" in dispatched[0]
    # Original user/assistant turns still present, role unchanged.
    assert any(m["role"] == "user" and "tell me about" in m["content"] for m in messages)
