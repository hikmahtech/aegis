"""POST /api/chat/agent-reply route — used by AgentChatReplyFlow's
synthesize_reply activity (worker → core).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from aegis.api.app import create_app
from aegis.api.auth import verify_auth
from aegis.api.deps import get_settings
from fastapi.testclient import TestClient


@pytest.fixture
def _client(test_settings):
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: test_settings
    app.dependency_overrides[verify_auth] = lambda: True
    app.state.db_pool = AsyncMock()
    return TestClient(app)


def test_post_chat_agent_reply_returns_200_with_reply(_client):
    fake = {
        "reply_text": "OK",
        "tool_trace_summary": "search_knowledge",
        "llm_model": "claude-sonnet",
        "error": None,
        "error_is_transient": False,
    }
    with patch(
        "aegis.api.routes.chat.synthesize_agent_reply",
        new=AsyncMock(return_value=fake),
    ):
        resp = _client.post(
            "/api/chat/agent-reply",
            json={
                "agent_id": "raphael",
                "message": "user comment",
                "thread_id": "todoist-task-abc",
                "task_id": "abc",
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["reply_text"] == "OK"
    assert body["tool_trace_summary"] == "search_knowledge"
    assert body["error"] is None


def test_post_chat_agent_reply_returns_5xx_on_synthesize_raise(_client):
    """Transient failure inside synthesize_agent_reply → 5xx so the
    worker activity retries.
    """
    import httpx

    raise_exc = httpx.HTTPStatusError(
        "503",
        request=httpx.Request("POST", "https://x"),
        response=httpx.Response(503, request=httpx.Request("POST", "https://x")),
    )
    with patch(
        "aegis.api.routes.chat.synthesize_agent_reply",
        new=AsyncMock(side_effect=raise_exc),
    ):
        resp = _client.post(
            "/api/chat/agent-reply",
            json={
                "agent_id": "raphael",
                "message": "x",
                "thread_id": "t",
                "task_id": "abc",
            },
        )
    assert 500 <= resp.status_code < 600


def test_post_chat_agent_reply_returns_200_with_error_on_agent_not_found(_client):
    """Permanent failure (agent not found) → 200 with error field — the
    activity does NOT retry these.
    """
    fake = {
        "reply_text": "",
        "tool_trace_summary": "",
        "llm_model": "",
        "error": "Agent 'foo' not found",
        "error_is_transient": False,
    }
    with patch(
        "aegis.api.routes.chat.synthesize_agent_reply",
        new=AsyncMock(return_value=fake),
    ):
        resp = _client.post(
            "/api/chat/agent-reply",
            json={
                "agent_id": "foo",
                "message": "x",
                "thread_id": "t",
                "task_id": "abc",
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["error"] == "Agent 'foo' not found"


def test_post_chat_agent_reply_route_threads_llm_to_send_message(_client):
    """End-to-end argument threading: route → synthesize_agent_reply → send_message.
    Regression guard for the bug caught by final PR review on #261:
    synthesize_agent_reply was called without llm_client, causing TypeError at runtime.
    """
    send_mock = AsyncMock(return_value={
        "response": "OK",
        "model": "claude-sonnet",
        "tool_calls": [],
    })
    with patch("aegis.services.chat.send_message", new=send_mock):
        resp = _client.post(
            "/api/chat/agent-reply",
            json={
                "agent_id": "raphael",
                "message": "x",
                "thread_id": "t",
                "task_id": "abc",
            },
        )
    assert resp.status_code == 200
    # Verify send_message was called with llm_client present (TypeError would
    # otherwise propagate as 500).
    assert "llm_client" in send_mock.call_args.kwargs


def test_post_chat_agent_reply_threads_remote_script_connector(_client):
    """The comment/DM path must forward app.state.remote_script_connector to
    send_message, or pandora's infra chat tools (list_services / inspect_service /
    aegis_self_diagnose) get a None connector and return "Remote script connector
    not available". Regression guard for that silent gap.
    """
    sentinel = object()
    _client.app.state.remote_script_connector = sentinel
    send_mock = AsyncMock(return_value={"response": "OK", "model": "m", "tool_calls": []})
    with patch("aegis.services.chat.send_message", new=send_mock):
        resp = _client.post(
            "/api/chat/agent-reply",
            json={"agent_id": "pandoras-actor", "message": "x", "thread_id": "t", "task_id": "abc"},
        )
    assert resp.status_code == 200
    assert send_mock.call_args.kwargs.get("remote_script_connector") is sentinel
