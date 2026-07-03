"""Tests: Slack deliver endpoint — reply_markup must not crash SlackAdapter.

FIX 1 regression lock: SlackAdapter.send_message and send_document now accept an
optional reply_markup kwarg (accepted-and-ignored; Slack renders buttons via Block
Kit send_card).  Before the fix, every non-system delivery under AEGIS_CHANNEL=slack
raised a TypeError because the /api/deliver/message endpoint always passes
reply_markup= from the request body.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def slack_send_result():
    """Minimal successful Slack send result dict (what AsyncWebClient returns)."""
    return {"ok": True, "ts": "1.2", "channel": "C1"}


@pytest.fixture()
def slack_app(monkeypatch, slack_send_result):
    """Delivery app wired with a SlackAdapter whose AsyncWebClient is mocked.

    _resolve is stubbed to return (channel_id="C1", username="AEGIS", icon=":gear:")
    so no HTTP calls hit the core API.
    """
    # pydantic-settings reads AliasChoices fields from env; set env vars first.
    monkeypatch.setenv("AEGIS_CHANNEL", "slack")
    monkeypatch.setenv("AEGIS_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("AEGIS_SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("AEGIS_API_KEY", "test-key")

    from aegis_comms.config import CommsSettings

    settings = CommsSettings(_env_file=None)

    from aegis_comms.__main__ import create_delivery_app
    from aegis_comms.adapters.slack import SlackAdapter

    adapter = SlackAdapter(settings)

    # Stub _resolve so we skip core-API + conversations_list calls
    async def _fake_resolve(agent_id: str):
        return ("C1", "AEGIS", ":gear:", "")

    adapter._resolve = _fake_resolve  # type: ignore[method-assign]

    # Mock the underlying AsyncWebClient.chat_postMessage
    mock_post = AsyncMock(return_value=slack_send_result)
    adapter._client.chat_postMessage = mock_post  # type: ignore[attr-defined]

    # Mock files_upload_v2 for document tests
    mock_upload = AsyncMock(return_value={"ok": True, "files": [{"ts": "1.2"}]})
    adapter._client.files_upload_v2 = mock_upload  # type: ignore[attr-defined]

    app = create_delivery_app(adapter, settings)

    # Stub _log_dispatch so there are no outbound core-API calls
    with patch("aegis_comms.__main__._log_dispatch", new=AsyncMock(return_value=None)):
        yield app, adapter


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_slack_deliver_without_reply_markup(slack_app):
    """Normal delivery (no reply_markup) succeeds under SlackAdapter."""
    app, adapter = slack_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/deliver/message",
            json={"text": "Hello from workflow", "agent_id": "sebas"},
            headers={"X-API-Key": "test-key"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True


async def test_slack_deliver_with_reply_markup_does_not_crash(slack_app):
    """Delivery WITH reply_markup must NOT raise TypeError under SlackAdapter.

    This is the regression that FIX 1 addresses: the deliver endpoint always
    passes reply_markup= to adapter.send_message; before the fix, SlackAdapter
    rejected the unexpected kwarg and every interaction-card delivery 500'd.
    """
    app, adapter = slack_app
    transport = ASGITransport(app=app)
    reply_markup = {
        "inline_keyboard": [[{"text": "Approve", "callback_data": "interaction_approve:abc"}]]
    }
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/deliver/message",
            json={
                "text": "Approve the fix?",
                "agent_id": "pandoras-actor",
                "reply_markup": reply_markup,
            },
            headers={"X-API-Key": "test-key"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    # Slack ignores the keyboard; the message is still sent via chat_postMessage
    adapter._client.chat_postMessage.assert_called_once()


async def test_slack_document_deliver_with_reply_markup_does_not_crash(slack_app):
    """Document delivery WITH reply_markup must NOT raise TypeError under SlackAdapter."""
    app, adapter = slack_app
    transport = ASGITransport(app=app)
    reply_markup = {
        "inline_keyboard": [[{"text": "✅ Approve", "callback_data": "task:approve_fix:abc"}]]
    }
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/deliver/document",
            json={
                "agent_id": "pandoras-actor",
                "caption": "Investigation complete",
                "documents": [{"filename": "fix.md", "content": "# Fix\n\nChanged Y"}],
                "reply_markup": reply_markup,
            },
            headers={"X-API-Key": "test-key"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["count"] == 1
    # Slack ignores the keyboard; file upload is still called
    adapter._client.files_upload_v2.assert_called_once()
