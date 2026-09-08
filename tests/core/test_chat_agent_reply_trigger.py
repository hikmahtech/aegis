"""Tests for the bot→core→temporal trigger route + the taskless agent-reply path."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock

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


async def test_agent_reply_trigger_creates_no_task(app, auth_headers, monkeypatch):
    """A chat ask is a conversation, not a chore: the route captures nothing
    and starts the flow taskless.

    The route used to capture every message as a `#chat` task before the flow
    ran. The capture core is patched here and asserted unused, so a
    re-introduction by any path fails rather than quietly filling the inbox.
    """
    capture = AsyncMock(return_value="task-should-not-exist")
    monkeypatch.setattr(
        "aegis.services.tools.gtd._capture_to_inbox_impl", capture, raising=True
    )

    fake_handle = MagicMock()
    fake_handle.id = "agent-chat-reply-dm-pandoras-actor-abc123"
    fake_temporal = MagicMock()
    fake_temporal.start_workflow = AsyncMock(return_value=fake_handle)
    app.state.temporal_client = fake_temporal

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/chat/agent-reply/trigger",
            headers=auth_headers,
            json={
                "target_agent": "pandoras-actor",
                "message": "why is gmail-ingest dropping emails?",
                "thread_id": "chat-12345-pandoras-actor",
                "reply_chat_id": 12345,
            },
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["target_agent"] == "pandoras-actor"
    assert body["workflow_id"].startswith("agent-chat-reply-dm-pandoras-actor-")
    assert body["task_id"] is None
    capture.assert_not_awaited()

    call = fake_temporal.start_workflow.call_args
    assert call.args[0] == "AgentChatReplyFlow"
    payload = call.args[1]
    assert payload["target_agent"] == "pandoras-actor"
    assert payload["task_id"] is None
    assert payload["reply_chat_id"] == 12345
    assert payload["thread_id"] == "chat-12345-pandoras-actor"
    assert call.kwargs["task_queue"] == "aegis-main"


async def test_agent_reply_trigger_repeat_asks_stay_taskless(app, auth_headers, monkeypatch):
    """Several turns in one conversation leave nothing behind.

    This is the failure the change fixes: the old thread-keyed capture reused
    one task until the user completed it, then minted a fresh task per message
    forever — 29 of them from a single Slack channel.
    """
    capture = AsyncMock(return_value="task-should-not-exist")
    monkeypatch.setattr(
        "aegis.services.tools.gtd._capture_to_inbox_impl", capture, raising=True
    )
    fake_temporal = MagicMock()
    fake_temporal.start_workflow = AsyncMock(return_value=MagicMock())
    app.state.temporal_client = fake_temporal

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for message in ("can we just restart it", "why is our read only?", "and now?"):
            resp = await client.post(
                "/api/chat/agent-reply/trigger",
                headers=auth_headers,
                json={
                    "target_agent": "sebas",
                    "message": message,
                    "thread_id": "slack-C0BBX9UN996-sebas",
                    "reply_chat_id": 9,
                },
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["task_id"] is None

    assert capture.await_count == 0
    assert fake_temporal.start_workflow.await_count == 3
    for call in fake_temporal.start_workflow.await_args_list:
        assert call.args[1]["task_id"] is None


async def test_agent_reply_trigger_503_when_temporal_unavailable(app, auth_headers):
    """If app.state.temporal_client is missing, return 503 so the bot can
    fall back to the synchronous /api/chat path."""
    # explicitly do NOT set temporal_client
    app.state.temporal_client = None

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/chat/agent-reply/trigger",
            headers=auth_headers,
            json={
                "target_agent": "pandoras-actor",
                "message": "ping",
                "thread_id": "x",
                "reply_chat_id": 1,
            },
        )

    assert resp.status_code == 503
    assert "temporal" in resp.json()["detail"].lower()


async def test_agent_reply_accepts_taskless_payload():
    """`/api/chat/agent-reply` (the existing worker→core endpoint) must accept
    a body with task_id=None (DM path); user_metadata.surface flips to
    `chat_dm` and the task_id key is omitted."""
    from aegis.services.chat import synthesize_agent_reply

    captured: dict = {}

    async def _fake_send_message(
        *, pool, llm_client, agent_id, message, thread_id, user_metadata, **kwargs
    ):
        captured["user_metadata"] = dict(user_metadata)
        captured["agent_id"] = agent_id
        return {
            "response": "Hello.",
            "tool_calls": [],
            "model": "claude-sonnet",
        }

    # Monkeypatch the inner send_message used by synthesize_agent_reply.
    import aegis.services.chat as chat_mod

    original = chat_mod.send_message
    chat_mod.send_message = _fake_send_message
    try:
        result = await synthesize_agent_reply(
            pool=MagicMock(),
            llm_client=MagicMock(),
            agent_id="pandoras-actor",
            message="hi",
            thread_id="dm-thread",
            task_id=None,
        )
    finally:
        chat_mod.send_message = original

    assert result["reply_text"] == "Hello."
    assert captured["user_metadata"]["surface"] == "chat_dm"
    assert "task_id" not in captured["user_metadata"], (
        "DM path must NOT carry a task_id in user_metadata"
    )


async def test_agent_reply_with_task_id_keeps_todoist_surface_tag():
    """The Todoist comment-channel path (task_id set) keeps `surface=todoist_comment`
    and includes task_id in user_metadata — regression-pinning the comment-channel
    surface tag introduced by PR #261."""
    from aegis.services.chat import synthesize_agent_reply

    captured: dict = {}

    async def _fake_send_message(
        *, pool, llm_client, agent_id, message, thread_id, user_metadata, **kwargs
    ):
        captured["user_metadata"] = dict(user_metadata)
        return {"response": "ok.", "tool_calls": [], "model": "claude-sonnet"}

    import aegis.services.chat as chat_mod

    original = chat_mod.send_message
    chat_mod.send_message = _fake_send_message
    try:
        await synthesize_agent_reply(
            pool=MagicMock(),
            llm_client=MagicMock(),
            agent_id="pandoras-actor",
            message="hi",
            thread_id="todoist-task-abc",
            task_id="abc",
        )
    finally:
        chat_mod.send_message = original

    assert captured["user_metadata"]["surface"] == "todoist_comment"
    assert captured["user_metadata"]["task_id"] == "abc"
