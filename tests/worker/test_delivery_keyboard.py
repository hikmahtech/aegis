"""Tests for inline keyboard delivery."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aegis_worker.activities.delivery import DeliveryActivities


@pytest.fixture
def delivery():
    return DeliveryActivities(telegram_url="http://telegram:8081", api_key="test-key")


def _patch_client(delivery: DeliveryActivities, response_json: dict) -> AsyncMock:
    """Replace the pooled client with a mock and return it for assertions."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = response_json
    mock_client = AsyncMock()
    mock_client.is_closed = False
    mock_client.post = AsyncMock(return_value=mock_resp)
    delivery._client = mock_client
    return mock_client


@pytest.mark.asyncio
async def test_send_telegram_with_keyboard(delivery):
    """send_telegram should include reply_markup in POST body when keyboard provided."""
    keyboard = {"inline_keyboard": [[{"text": "Test", "callback_data": "test:1"}]]}
    mock_client = _patch_client(delivery, {"ok": True})

    result = await delivery.send_telegram("sebas", "Test message", 0, keyboard)

    assert result == {"ok": True}
    body = mock_client.post.call_args.kwargs["json"]
    assert body["reply_markup"] == keyboard


@pytest.mark.asyncio
async def test_send_telegram_without_keyboard(delivery):
    """send_telegram should not include reply_markup when keyboard is None."""
    mock_client = _patch_client(delivery, {"ok": True})

    await delivery.send_telegram("sebas", "Test message")

    body = mock_client.post.call_args.kwargs["json"]
    assert "reply_markup" not in body


@pytest.mark.asyncio
async def test_send_telegram_document(delivery):
    """send_telegram_document should POST to the document endpoint with caption, docs, keyboard."""
    keyboard = {"inline_keyboard": [[{"text": "✅", "callback_data": "task:approve_fix:abc"}]]}
    docs = [
        {"filename": "investigation-abc.md", "content": "# Findings\nRoot cause is X"},
        {"filename": "fix-abc.md", "content": "# Fix\nChanged Y"},
    ]

    mock_client = _patch_client(delivery, {"ok": True, "count": 2})

    result = await delivery.send_telegram_document(
        "pandoras-actor", docs, caption="Investigation complete", chat_id=123, keyboard=keyboard
    )

    assert result == {"ok": True, "count": 2}
    path = mock_client.post.call_args.args[0]
    assert path == "/api/deliver/telegram/document"
    body = mock_client.post.call_args.kwargs["json"]
    assert body["agent_id"] == "pandoras-actor"
    assert body["caption"] == "Investigation complete"
    assert body["reply_markup"] == keyboard
    assert len(body["documents"]) == 2
    assert body["documents"][0]["filename"] == "investigation-abc.md"


@pytest.mark.asyncio
async def test_send_telegram_document_no_url(delivery):
    """send_telegram_document returns error when telegram_url not configured."""
    delivery.telegram_url = ""
    result = await delivery.send_telegram_document("sebas", [{"filename": "a.md", "content": "x"}])
    assert result["ok"] is False
    assert "telegram_url" in result["error"]


@pytest.mark.asyncio
async def test_client_pool_reuses_single_instance(delivery):
    """_ensure_client should return the SAME client across calls, confirming the pool."""
    client1 = delivery._ensure_client()
    client2 = delivery._ensure_client()
    assert client1 is client2


@pytest.mark.asyncio
async def test_client_pool_recreates_after_close(delivery):
    """If the pooled client is closed, the next call creates a fresh one."""
    client1 = delivery._ensure_client()
    await client1.aclose()
    client2 = delivery._ensure_client()
    assert client2 is not client1
    assert not client2.is_closed
    await client2.aclose()


@pytest.mark.asyncio
async def test_safe_send_telegram_logs_raised_exception(monkeypatch):
    """Helper must log + swallow when send_telegram raises."""
    from aegis_worker.activities import delivery as delivery_mod

    captured: list[tuple[str, dict]] = []

    class _Recorder:
        def warning(self, event, **kw):
            captured.append((event, kw))

    monkeypatch.setattr(delivery_mod, "_logger", _Recorder())

    delivery = AsyncMock()
    delivery.db_pool = None  # no budget gate in these log-behaviour tests
    delivery.channel = "slack"  # exercise the slack send path
    delivery.send_telegram = AsyncMock(side_effect=RuntimeError("boom"))
    await delivery_mod.safe_send_telegram(
        delivery, agent_id="pandoras-actor", message="x", log_event="probe_failed"
    )
    assert captured == [("probe_failed", {"error": "boom", "reason": "raised"})]


@pytest.mark.asyncio
async def test_safe_send_telegram_logs_ok_false_dict(monkeypatch):
    """Helper must treat {ok: false} dict returns as failure (same class of silent
    failure that hid the 422 bug pre-PR #257)."""
    from aegis_worker.activities import delivery as delivery_mod

    captured: list[tuple[str, dict]] = []

    class _Recorder:
        def warning(self, event, **kw):
            captured.append((event, kw))

    monkeypatch.setattr(delivery_mod, "_logger", _Recorder())

    delivery = AsyncMock()
    delivery.db_pool = None  # no budget gate in these log-behaviour tests
    delivery.channel = "slack"  # exercise the slack send path
    delivery.send_telegram = AsyncMock(return_value={"ok": False, "error": "rate limit"})
    await delivery_mod.safe_send_telegram(
        delivery, agent_id="maou", message="x", log_event="probe_failed"
    )
    assert captured == [("probe_failed", {"error": "rate limit", "reason": "ok_false"})]


@pytest.mark.asyncio
async def test_safe_send_telegram_silent_on_ok_true(monkeypatch):
    """Helper must NOT log when the bot returns ok=true."""
    from aegis_worker.activities import delivery as delivery_mod

    captured: list = []

    class _Recorder:
        def warning(self, event, **kw):
            captured.append(event)

    monkeypatch.setattr(delivery_mod, "_logger", _Recorder())

    delivery = AsyncMock()
    delivery.db_pool = None  # no budget gate in these log-behaviour tests
    delivery.channel = "slack"  # exercise the slack send path
    delivery.send_telegram = AsyncMock(return_value={"ok": True, "message_id": 1})
    await delivery_mod.safe_send_telegram(
        delivery, agent_id="raphael", message="x", log_event="probe_failed"
    )
    assert captured == []
