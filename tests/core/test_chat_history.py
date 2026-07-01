"""Tests for chat history endpoints."""

import base64

import pytest
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def app(test_settings, mock_db_pool):
    application = create_app(run_lifespan=False)
    application.dependency_overrides[get_settings] = lambda: test_settings
    application.state.db_pool = mock_db_pool
    return application


@pytest.fixture
def auth_headers():
    creds = base64.b64encode(b"admin:admin").decode()
    return {"Authorization": f"Basic {creds}"}


async def test_list_threads(app, auth_headers, mock_db_pool):
    mock_db_pool.fetch.return_value = [
        {
            "agent_id": "sebas",
            "thread_id": "thread-1",
            "message_count": 10,
            "first_message": "2026-03-15T10:00:00Z",
            "last_message": "2026-03-16T12:00:00Z",
        },
        {
            "agent_id": "raphael",
            "thread_id": "thread-2",
            "message_count": 5,
            "first_message": "2026-03-14T08:00:00Z",
            "last_message": "2026-03-15T09:00:00Z",
        },
    ]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/chat/threads", headers=auth_headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 2


async def test_list_threads_with_agent_filter(app, auth_headers, mock_db_pool):
    mock_db_pool.fetch.return_value = [
        {
            "agent_id": "sebas",
            "thread_id": "thread-1",
            "message_count": 10,
            "first_message": "2026-03-15T10:00:00Z",
            "last_message": "2026-03-16T12:00:00Z",
        },
    ]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/chat/threads?agent_id=sebas", headers=auth_headers)
        assert resp.status_code == 200
        call_args = mock_db_pool.fetch.call_args
        query = call_args[0][0]
        assert "agent_id = $1" in query


async def test_get_thread_history(app, auth_headers, mock_db_pool):
    mock_db_pool.fetch.return_value = [
        {
            "id": "uuid-1",
            "agent_id": "sebas",
            "thread_id": "thread-1",
            "role": "user",
            "content": "Hello",
            "metadata": "{}",
            "created_at": "2026-03-16T10:00:00Z",
        },
        {
            "id": "uuid-2",
            "agent_id": "sebas",
            "thread_id": "thread-1",
            "role": "assistant",
            "content": "Hi there!",
            "metadata": "{}",
            "created_at": "2026-03-16T10:00:01Z",
        },
    ]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/chat/history?thread_id=thread-1", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["role"] == "user"
        assert data[1]["role"] == "assistant"


async def test_get_thread_history_missing_thread_id(app, auth_headers):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/chat/history", headers=auth_headers)
        assert resp.status_code == 400


async def test_log_dispatch_inserts_role_dispatch_row(app, auth_headers, mock_db_pool):
    """POST /api/chat/dispatches persists an outbound Telegram message as
    a chat_history row with role='dispatch'. Closes the gap where the
    user could see a briefing on Telegram that the agent's chat
    context didn't know about. The metadata carries the
    telegram_message_id + chat_id needed by the cleanup activity to
    later deleteMessage via Bot API."""
    payload = {
        "agent_id": "pandoras-actor",
        "topic_id": 2755,
        "chat_id": -1003528071837,
        "message_id": 8888,
        "content": "🔍 Investigation complete — root cause: foo",
        "kind": "interaction_card",
        "used_html": True,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/chat/dispatches", json=payload, headers=auth_headers
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    mock_db_pool.execute.assert_called_once()
    sql, *params = mock_db_pool.execute.call_args[0]
    assert "INSERT INTO chat_history" in sql
    assert "'dispatch'" in sql
    agent, thread_id, content, metadata = params
    assert agent == "pandoras-actor"
    assert thread_id == "2755"
    assert content.startswith("🔍 Investigation complete")
    assert metadata["telegram_message_id"] == 8888
    assert metadata["chat_id"] == -1003528071837
    assert metadata["kind"] == "interaction_card"


async def test_log_dispatch_falls_back_to_system_thread_id(
    app, auth_headers, mock_db_pool
):
    """System-event dispatches have no topic_id (they land in General).
    Persist them on the synthetic 'system' thread so retention + cleanup
    still apply."""
    payload = {
        "agent_id": "system",
        "topic_id": None,
        "chat_id": -1003528071837,
        "message_id": 9999,
        "content": "🏥 Worker started — 23 schedules synced",
        "kind": "system_event",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/chat/dispatches", json=payload, headers=auth_headers
        )
        assert resp.status_code == 200

    _sql, agent, thread_id, _content, _meta = mock_db_pool.execute.call_args[0]
    assert agent == "system"
    assert thread_id == "system"


async def test_log_dispatch_rejects_missing_content(app, auth_headers, mock_db_pool):
    """Without content there's nothing to log — fail fast so callers can
    tell the dispatch was not recorded rather than silently dropping it."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/chat/dispatches",
            json={"agent_id": "sebas", "topic_id": 2753, "message_id": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 400
    mock_db_pool.execute.assert_not_called()
