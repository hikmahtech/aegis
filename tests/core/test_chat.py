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
    """role='dispatch' rows are messages the user saw in chat (briefings,
    interaction cards, alert verdicts). The chat loader must surface them
    to the LLM as assistant turns with a "[Sent to you in chat]"
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
    # assistant turn with the [Sent to you in chat] prefix.
    assistant_contents = [m["content"] for m in messages if m["role"] == "assistant"]
    dispatched = [c for c in assistant_contents if "[Sent to you in chat]" in c]
    assert dispatched, f"dispatch never surfaced to LLM: {assistant_contents!r}"
    assert "Morning briefing" in dispatched[0]
    # Original user/assistant turns still present, role unchanged.
    assert any(m["role"] == "user" and "tell me about" in m["content"] for m in messages)


async def test_chat_history_caps_turns_and_dispatches_separately(
    app, auth_headers, mock_db_pool, mock_llm
):
    """#638: an agent's channel carries far more notices than conversation, so
    one shared LIMIT would let a busy hour of PR/alert posts push the user's
    own last turns out of the window. Turns and dispatches must be capped on
    their own, and a dispatch counts as context only while recent."""
    from aegis.services import chat as chat_mod

    mock_db_pool.fetchrow.return_value = {
        "id": "pandoras-actor",
        "name": "Pandora",
        "system_prompt_path": "personalities/pandoras-actor",
        "role": "infrastructure",
    }
    mock_db_pool.fetch.return_value = []

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/chat",
            headers=auth_headers,
            json={"agent_id": "pandoras-actor", "thread_id": "slack-C1-pandoras-actor", "message": "hi"},
        )
        assert resp.status_code == 200

    history_calls = [
        c for c in mock_db_pool.fetch.call_args_list if "FROM chat_history" in c.args[0]
    ]
    assert history_calls, "history was never loaded"
    sql, *params = history_calls[0].args
    # Two sub-selects: one over the conversation, one over dispatches only.
    assert sql.count("FROM chat_history") == 2
    assert "role <> 'dispatch'" in sql and "role = 'dispatch'" in sql
    assert "make_interval" in sql
    assert params[:2] == ["pandoras-actor", "slack-C1-pandoras-actor"]
    assert params[2:] == [
        chat_mod._HISTORY_TURNS,
        chat_mod._HISTORY_DISPATCHES,
        chat_mod._HISTORY_DISPATCH_HOURS,
    ]


async def test_chat_shows_an_async_reply_once(app, auth_headers, mock_db_pool, mock_llm):
    """An async reply is saved as an assistant row by send_message AND logged
    as a dispatch by comms when it is posted (#638 puts both in the same
    thread). The model must see it once, as the assistant turn."""
    mock_db_pool.fetchrow.return_value = {
        "id": "pandoras-actor",
        "name": "Pandora",
        "system_prompt_path": "personalities/pandoras-actor",
        "role": "infrastructure",
    }
    reply = "Dispatched, owner-sama. Results land here."
    mock_db_pool.fetch.return_value = [
        {"role": "user", "content": "dispatch the run"},
        {"role": "assistant", "content": reply},
        {"role": "dispatch", "content": reply},
        {"role": "dispatch", "content": "PR opened: #434"},
    ][::-1]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/chat",
            headers=auth_headers,
            json={"agent_id": "pandoras-actor", "thread_id": "slack-C1-pandoras-actor", "message": "this is merged"},
        )
        assert resp.status_code == 200

    messages = mock_llm.chat.call_args.kwargs["messages"]
    with_reply = [m for m in messages if m["role"] == "assistant" and reply in m["content"]]
    assert len(with_reply) == 1, with_reply
    assert "[Sent to you in chat]" not in with_reply[0]["content"]
    assert any(m["content"] == "[Sent to you in chat]\nPR opened: #434" for m in messages)
