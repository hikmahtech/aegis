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


async def test_agent_reply_trigger_captures_task_and_anchors_workflow(
    app, auth_headers, monkeypatch
):
    """POST /api/chat/agent-reply/trigger captures the ask as a `#telegram`
    Todoist task owned by the agent, then starts AgentChatReplyFlow anchored to
    that task id (so the reply + any spawned workflow land on it)."""
    captured: dict = {}

    async def _fake_capture(*, pool, source_tag, external_id, title, description, extra_labels):
        captured.update(
            source_tag=source_tag,
            external_id=external_id,
            title=title,
            extra_labels=extra_labels,
        )
        return "real-task-123"

    monkeypatch.setattr("aegis.api.routes.chat._capture_to_inbox_impl", _fake_capture, raising=True)
    # Freshly-created task isn't in the projection yet → treated as open.
    monkeypatch.setattr(
        "aegis.api.routes.chat._task_is_completed",
        AsyncMock(return_value=False),
        raising=True,
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
                "thread_id": "telegram-12345-pandoras-actor",
                "reply_chat_id": 12345,
            },
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["target_agent"] == "pandoras-actor"
    assert body["workflow_id"].startswith("agent-chat-reply-dm-pandoras-actor-")
    assert body["task_id"] == "real-task-123"

    # Capture tagged the task #telegram + owned by the agent, keyed on the DM
    # thread so a multi-turn conversation maps to one task.
    assert captured["source_tag"] == "#telegram"
    assert captured["extra_labels"] == ["@pandora"]
    assert captured["title"] == "why is gmail-ingest dropping emails?"
    assert captured["external_id"] == "tg-chat:telegram-12345-pandoras-actor"

    # Workflow anchored to the captured task.
    call = fake_temporal.start_workflow.call_args
    assert call.args[0] == "AgentChatReplyFlow"
    payload = call.args[1]
    assert payload["target_agent"] == "pandoras-actor"
    assert payload["task_id"] == "real-task-123"
    assert payload["reply_chat_id"] == 12345
    assert payload["thread_id"] == "telegram-12345-pandoras-actor"
    assert call.kwargs["task_queue"] == "aegis-main"


async def test_agent_reply_trigger_taskless_when_capture_unavailable(
    app, auth_headers, monkeypatch
):
    """Capture returning None (kill-switch off / no inbox / no api key) →
    workflow runs taskless (reply still delivered)."""
    monkeypatch.setattr(
        "aegis.api.routes.chat._capture_to_inbox_impl",
        AsyncMock(return_value=None),
        raising=True,
    )
    fake_temporal = MagicMock()
    fake_temporal.start_workflow = AsyncMock(return_value=MagicMock())
    app.state.temporal_client = fake_temporal

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/chat/agent-reply/trigger",
            headers=auth_headers,
            json={
                "target_agent": "sebas",
                "message": "remind me later",
                "thread_id": "telegram-9-sebas",
                "reply_chat_id": 9,
            },
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["task_id"] is None
    payload = fake_temporal.start_workflow.call_args.args[1]
    assert payload["task_id"] is None


async def test_agent_reply_trigger_taskless_on_outbox_temp_id(app, auth_headers, monkeypatch):
    """A transient capture that returns an outbox temp-id ("item-…") can't take
    comments yet → stay taskless rather than anchoring to an unusable id."""
    monkeypatch.setattr(
        "aegis.api.routes.chat._capture_to_inbox_impl",
        AsyncMock(return_value="item-abc-temp"),
        raising=True,
    )
    fake_temporal = MagicMock()
    fake_temporal.start_workflow = AsyncMock(return_value=MagicMock())
    app.state.temporal_client = fake_temporal

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/chat/agent-reply/trigger",
            headers=auth_headers,
            json={
                "target_agent": "raphael",
                "message": "x",
                "thread_id": "t",
                "reply_chat_id": 1,
            },
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["task_id"] is None
    assert fake_temporal.start_workflow.call_args.args[1]["task_id"] is None


async def test_agent_reply_trigger_recaptures_when_thread_task_completed(app, auth_headers, monkeypatch):
    """If the thread's reused task has been completed, anchor to a freshly
    captured task instead of mirroring onto one the user no longer sees."""
    external_ids: list[str] = []

    async def _fake_capture(*, pool, source_tag, external_id, title, description, extra_labels):
        external_ids.append(external_id)
        # First call (thread-keyed) returns the stale/completed task; the
        # re-capture (suffixed key) returns a fresh one.
        return "stale-done-task" if len(external_ids) == 1 else "fresh-task-2"

    monkeypatch.setattr(
        "aegis.api.routes.chat._capture_to_inbox_impl", _fake_capture, raising=True
    )
    monkeypatch.setattr(
        "aegis.api.routes.chat._task_is_completed",
        AsyncMock(return_value=True),
        raising=True,
    )
    fake_temporal = MagicMock()
    fake_temporal.start_workflow = AsyncMock(return_value=MagicMock())
    app.state.temporal_client = fake_temporal

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/chat/agent-reply/trigger",
            headers=auth_headers,
            json={
                "target_agent": "pandoras-actor",
                "message": "back again",
                "thread_id": "telegram-7-pandoras-actor",
                "reply_chat_id": 7,
            },
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["task_id"] == "fresh-task-2"
    # Thread key first, then a uniquely-suffixed key for the fresh task.
    assert external_ids[0] == "tg-chat:telegram-7-pandoras-actor"
    assert external_ids[1].startswith("tg-chat:telegram-7-pandoras-actor:")
    assert fake_temporal.start_workflow.call_args.args[1]["task_id"] == "fresh-task-2"


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
    `telegram_dm` and the task_id key is omitted."""
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
    assert captured["user_metadata"]["surface"] == "telegram_dm"
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
