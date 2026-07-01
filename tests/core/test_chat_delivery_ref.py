"""Tests for channel-neutral delivery_ref on chat routes (Slice 5a).

All changes are additive — the existing telegram paths are verified
to keep working unchanged (dormant fallback channel).
"""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock

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


# ---------------------------------------------------------------------------
# POST /api/chat/dispatches — delivery_ref stored alongside existing keys
# ---------------------------------------------------------------------------


async def test_log_dispatch_with_delivery_ref_stores_it_in_metadata(
    app, auth_headers, mock_db_pool
):
    """A Slack dispatch sends delivery_ref; the inserted metadata must include it.

    The cleanup activity reads metadata.delivery_ref to delete the Slack
    message — it must round-trip the dict exactly as supplied.
    """
    payload = {
        "agent_id": "pandoras-actor",
        "topic_id": 2755,
        "content": "Alert: foo is down",
        "kind": "interaction_card",
        "delivery_ref": {"adapter": "slack", "channel": "C1234567890", "ts": "1718000000.123456"},
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/chat/dispatches", json=payload, headers=auth_headers
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    mock_db_pool.execute.assert_called_once()
    _sql, agent, thread_id, content, metadata = mock_db_pool.execute.call_args[0]
    assert agent == "pandoras-actor"
    assert thread_id == "2755"
    assert metadata["delivery_ref"] == {
        "adapter": "slack",
        "channel": "C1234567890",
        "ts": "1718000000.123456",
    }
    # Existing keys still present for back-compat
    assert "kind" in metadata


async def test_log_dispatch_without_delivery_ref_keeps_telegram_shape(
    app, auth_headers, mock_db_pool
):
    """Telegram callers don't send delivery_ref — metadata shape unchanged."""
    payload = {
        "agent_id": "pandoras-actor",
        "topic_id": 2755,
        "chat_id": -1003528071837,
        "message_id": 8888,
        "content": "Briefing sent",
        "kind": "deliver",
        "used_html": True,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/chat/dispatches", json=payload, headers=auth_headers
        )
    assert resp.status_code == 200

    _sql, _agent, _thread, _content, metadata = mock_db_pool.execute.call_args[0]
    assert metadata["telegram_message_id"] == 8888
    assert metadata["chat_id"] == -1003528071837
    # delivery_ref must not appear when not supplied
    assert "delivery_ref" not in metadata


# ---------------------------------------------------------------------------
# POST /api/chat — delivery_ref block on user row
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_llm():
    llm = AsyncMock()
    llm.chat = AsyncMock(
        return_value={
            "response": "Hello!",
            "tool_calls": [],
            "model": "test-model",
            "prompt_tokens": 10,
            "completion_tokens": 5,
        }
    )
    llm.close = AsyncMock()
    return llm


@pytest.fixture
def chat_app(test_settings, mock_db_pool, mock_llm):
    application = create_app(run_lifespan=False)
    application.dependency_overrides[get_settings] = lambda: test_settings
    application.state.db_pool = mock_db_pool
    application.state.llm = mock_llm
    return application


async def test_chat_with_delivery_ref_stores_it_on_user_row(
    chat_app, auth_headers, mock_db_pool
):
    """POST /api/chat with a delivery_ref block stores it in the user row's metadata.

    The Slack inbound path supplies delivery_ref instead of the telegram block.
    The user chat_history row's metadata must carry delivery_ref so the
    assistant can later reference the original message for reactions/replies.
    """
    mock_db_pool.fetchrow.return_value = {
        "id": "sebas",
        "name": "Sebas",
        "system_prompt_path": "personalities/sebas/SOUL.md",
        "role": "executive-assistant",
    }
    mock_db_pool.fetch.return_value = []

    delivery_ref = {"adapter": "slack", "channel": "C1234567890", "ts": "1718000001.000000"}

    async with AsyncClient(transport=ASGITransport(app=chat_app), base_url="http://test") as client:
        resp = await client.post(
            "/api/chat",
            headers=auth_headers,
            json={
                "agent_id": "sebas",
                "message": "What tasks do I have?",
                "delivery_ref": delivery_ref,
            },
        )
    assert resp.status_code == 200

    # Find the execute() call that inserted the user row.
    # SQL: INSERT INTO chat_history (...) VALUES ($1, $2, $3, $4, $5)
    # Params: agent_id, thread_id, "user", message, user_metadata
    user_insert_calls = [
        call
        for call in mock_db_pool.execute.call_args_list
        if len(call[0]) >= 4
        and "INSERT INTO chat_history" in call[0][0]
        and call[0][3] == "user"  # role param
    ]
    assert user_insert_calls, "no user row INSERT found"
    call_args = user_insert_calls[0][0]
    # call_args = (sql, agent_id, thread_id, "user", message[, metadata])
    user_metadata = call_args[5] if len(call_args) > 5 else None
    assert user_metadata is not None, "user row must have metadata (delivery_ref path)"
    assert user_metadata.get("delivery_ref") == delivery_ref
    assert user_metadata.get("kind") == "user_message"


async def test_chat_with_telegram_block_keeps_legacy_shape(
    chat_app, auth_headers, mock_db_pool
):
    """Telegram callers use the telegram:{chat_id,message_id} block — unchanged."""
    mock_db_pool.fetchrow.return_value = {
        "id": "sebas",
        "name": "Sebas",
        "system_prompt_path": "personalities/sebas/SOUL.md",
        "role": "executive-assistant",
    }
    mock_db_pool.fetch.return_value = []

    async with AsyncClient(transport=ASGITransport(app=chat_app), base_url="http://test") as client:
        resp = await client.post(
            "/api/chat",
            headers=auth_headers,
            json={
                "agent_id": "sebas",
                "message": "Hello",
                "telegram": {"chat_id": -1003528071837, "message_id": 1234},
            },
        )
    assert resp.status_code == 200

    user_insert_calls = [
        call
        for call in mock_db_pool.execute.call_args_list
        if len(call[0]) >= 4
        and "INSERT INTO chat_history" in call[0][0]
        and call[0][3] == "user"
    ]
    assert user_insert_calls, "no user row INSERT found"
    call_args = user_insert_calls[0][0]
    user_metadata = call_args[5] if len(call_args) > 5 else None
    assert user_metadata is not None, "user row must carry telegram metadata"
    assert user_metadata.get("telegram_message_id") == 1234
    assert user_metadata.get("chat_id") == -1003528071837
    assert "delivery_ref" not in user_metadata


async def test_chat_delivery_ref_takes_precedence_over_telegram(
    chat_app, auth_headers, mock_db_pool
):
    """If both delivery_ref and telegram are supplied, delivery_ref wins."""
    mock_db_pool.fetchrow.return_value = {
        "id": "sebas",
        "name": "Sebas",
        "system_prompt_path": "personalities/sebas/SOUL.md",
        "role": "executive-assistant",
    }
    mock_db_pool.fetch.return_value = []

    delivery_ref = {"adapter": "slack", "channel": "C9999", "ts": "9.0"}
    async with AsyncClient(transport=ASGITransport(app=chat_app), base_url="http://test") as client:
        resp = await client.post(
            "/api/chat",
            headers=auth_headers,
            json={
                "agent_id": "sebas",
                "message": "Both blocks",
                "delivery_ref": delivery_ref,
                "telegram": {"chat_id": -1, "message_id": 999},
            },
        )
    assert resp.status_code == 200

    user_insert_calls = [
        call
        for call in mock_db_pool.execute.call_args_list
        if len(call[0]) >= 4
        and "INSERT INTO chat_history" in call[0][0]
        and call[0][3] == "user"
    ]
    assert user_insert_calls, "no user row INSERT found"
    call_args = user_insert_calls[0][0]
    user_metadata = call_args[5] if len(call_args) > 5 else None
    assert user_metadata is not None, "delivery_ref path must set user_metadata"
    assert user_metadata["delivery_ref"] == delivery_ref
    # telegram keys must NOT be present when delivery_ref won
    assert "telegram_message_id" not in user_metadata


# ---------------------------------------------------------------------------
# POST /api/chat/messages/{id}/delivery-ref — neutral patch endpoint
# ---------------------------------------------------------------------------


async def test_patch_delivery_ref_patches_row_metadata(app, auth_headers, mock_db_pool):
    """POST /api/chat/messages/{id}/delivery-ref patches the row's metadata.delivery_ref."""
    mock_db_pool.execute.return_value = "UPDATE 1"
    row_id = "00000000-0000-0000-0000-000000000abc"
    delivery_ref = {"adapter": "slack", "channel": "C1111", "ts": "1.5"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/api/chat/messages/{row_id}/delivery-ref",
            json={"delivery_ref": delivery_ref},
            headers=auth_headers,
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    mock_db_pool.execute.assert_called_once()
    sql, ref_json, target_id = mock_db_pool.execute.call_args[0]
    assert "UPDATE chat_history" in sql
    assert "delivery_ref" in sql
    assert ref_json == delivery_ref
    assert target_id == row_id


async def test_patch_delivery_ref_404_on_missing_row(app, auth_headers, mock_db_pool):
    """404 when no row matches — caller must know the patch was not applied."""
    mock_db_pool.execute.return_value = "UPDATE 0"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/chat/messages/00000000-0000-0000-0000-000000000999/delivery-ref",
            json={"delivery_ref": {"adapter": "slack", "channel": "C1", "ts": "1.0"}},
            headers=auth_headers,
        )
    assert resp.status_code == 404


async def test_patch_delivery_ref_400_on_missing_body(app, auth_headers, mock_db_pool):
    """400 when delivery_ref key is absent from the body."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/chat/messages/00000000-0000-0000-0000-000000000123/delivery-ref",
            json={"wrong_key": "oops"},
            headers=auth_headers,
        )
    assert resp.status_code == 400
    mock_db_pool.execute.assert_not_called()
