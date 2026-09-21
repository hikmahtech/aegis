"""Channel-neutral delivery surface tests.

The per-channel delivery behaviour (Slack send / document / health) lives in
test_delivery_slack.py and test_inbound_health.py. This module keeps only the
channel-agnostic pieces: the DeliveryRequest model and the neutral
delivery_ref forwarding in _log_dispatch.
"""


async def test_log_dispatch_forwards_neutral_delivery_ref(monkeypatch):
    """_log_dispatch forwards the neutral delivery_ref block from the send
    result into the /api/chat/dispatches POST body (Slack ref), alongside the
    legacy top-level keys when present."""
    import aegis_comms.__main__ as bot_main
    from aegis_comms.config import CommsSettings

    settings = CommsSettings(
        _env_file=None,
        core_url="http://core.test",
        api_key="k",
        admin_username="",
    )

    captured: dict = {}

    class _FakeResp:
        status_code = 200

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None, auth=None):
            captured["url"] = url
            captured["json"] = json
            return _FakeResp()

    monkeypatch.setattr(bot_main.httpx, "AsyncClient", _FakeClient)

    send_result = {
        "ok": True,
        "used_html": False,
        "delivery_ref": {"adapter": "slack", "channel": "CSEBAS", "ts": "9.9"},
        # legacy mirror that SendResult.to_response() also emits
        "channel": "CSEBAS",
        "ts": "9.9",
    }
    await bot_main._log_dispatch(
        settings,
        agent_id="sebas",
        content="hello",
        send_result=send_result,
        kind="deliver",
    )

    assert captured["url"].endswith("/api/chat/dispatches")
    body = captured["json"]
    assert body["delivery_ref"] == {"adapter": "slack", "channel": "CSEBAS", "ts": "9.9"}
    assert body["agent_id"] == "sebas"
    assert body["content"] == "hello"
    assert body["kind"] == "deliver"
    # #638: the dispatch is filed under the thread the inbound handler uses for
    # this channel and agent, so the chat loader can find it when the user
    # replies. Before this it went under `system` and was never seen.
    from aegis_comms.adapters.slack import slack_thread_id

    assert body["thread_id"] == slack_thread_id("CSEBAS", "sebas") == "slack-CSEBAS-sebas"


def _capture_dispatch(monkeypatch):
    import aegis_comms.__main__ as bot_main

    captured: dict = {}

    class _FakeResp:
        status_code = 200

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None, auth=None):
            captured["json"] = json
            return _FakeResp()

    monkeypatch.setattr(bot_main.httpx, "AsyncClient", _FakeClient)
    return captured


async def test_log_dispatch_system_event_stays_threadless(monkeypatch):
    """A system event is posted as AEGIS, not as an agent, so it has no
    conversation to join: thread_id is None and core files it under `system`."""
    import aegis_comms.__main__ as bot_main
    from aegis_comms.config import CommsSettings

    settings = CommsSettings(_env_file=None, core_url="http://core.test", api_key="k", admin_username="")
    captured = _capture_dispatch(monkeypatch)
    await bot_main._log_dispatch(
        settings,
        agent_id="system",
        content="worker restarted",
        send_result={
            "ok": True,
            "delivery_ref": {"adapter": "slack", "channel": "CGENERAL", "ts": "1.1"},
        },
        kind="system_event",
    )
    assert captured["json"]["thread_id"] is None


async def test_log_dispatch_without_channel_stays_threadless(monkeypatch):
    """No channel in the ref (a non-Slack adapter, or a failed ref) means no
    thread can be named; the row must still be logged, threadless."""
    import aegis_comms.__main__ as bot_main
    from aegis_comms.config import CommsSettings

    settings = CommsSettings(_env_file=None, core_url="http://core.test", api_key="k", admin_username="")
    captured = _capture_dispatch(monkeypatch)
    await bot_main._log_dispatch(
        settings,
        agent_id="sebas",
        content="hello",
        send_result={"ok": True, "delivery_ref": {"adapter": "slack"}},
        kind="deliver",
    )
    assert captured["json"]["thread_id"] is None
    assert captured["json"]["content"] == "hello"


def _thread_app(monkeypatch):
    """Delivery app whose adapter is a mock, so the route's own forwarding is
    what the assertions see. `core_url=""` short-circuits `_log_dispatch`."""
    from unittest.mock import AsyncMock

    from aegis_comms.__main__ import create_delivery_app
    from aegis_comms.adapters.base import DeliveryRef, SendResult
    from aegis_comms.config import CommsSettings

    # Alias keys, not field names — the fields are AEGIS_*-aliased. A blank
    # core_url short-circuits _log_dispatch before it makes any HTTP call.
    settings = CommsSettings(_env_file=None, AEGIS_CORE_URL="", AEGIS_API_KEY="")
    adapter = AsyncMock()
    adapter.send_message.return_value = SendResult(
        ok=True, ref=DeliveryRef("slack", {"channel": "CTASK", "ts": "200.2"}), used_html=False
    )
    return create_delivery_app(adapter, settings), adapter


async def test_deliver_message_forwards_thread_ref_as_target(monkeypatch):
    """`thread_ref` (the thread ROOT) becomes the adapter's thread target."""
    from httpx import ASGITransport, AsyncClient

    app, adapter = _thread_app(monkeypatch)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/deliver/message",
            json={
                "text": "turn 2 finished",
                "agent_id": "sebas",
                "thread_ref": {"channel": "CTASK", "ts": "100.1"},
            },
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    kwargs = adapter.send_message.await_args.kwargs
    assert kwargs["target"] == {"channel": "CTASK", "thread_ts": "100.1"}


async def test_deliver_message_without_thread_ref_has_no_target(monkeypatch):
    from httpx import ASGITransport, AsyncClient

    app, adapter = _thread_app(monkeypatch)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/deliver/message", json={"text": "hi", "agent_id": "sebas"}
        )

    assert resp.status_code == 200, resp.text
    assert adapter.send_message.await_args.kwargs["target"] is None


async def test_deliver_message_forwards_thread_overflow_with_no_root(monkeypatch):
    """A message that OPENS a thread has no `thread_ref` yet, so the flag is the
    only thing telling the adapter to keep the chunks together."""
    from httpx import ASGITransport, AsyncClient

    app, adapter = _thread_app(monkeypatch)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/deliver/message",
            json={"text": "turn 1 finished", "agent_id": "sebas", "thread_overflow": True},
        )

    assert resp.status_code == 200, resp.text
    # No channel key: the adapter resolves the agent's own channel, as ever.
    assert adapter.send_message.await_args.kwargs["target"] == {"thread_overflow": True}


async def test_deliver_message_keeps_the_root_when_both_are_given(monkeypatch):
    from httpx import ASGITransport, AsyncClient

    app, adapter = _thread_app(monkeypatch)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/deliver/message",
            json={
                "text": "turn 2 finished",
                "agent_id": "sebas",
                "thread_ref": {"channel": "CTASK", "ts": "100.1"},
                "thread_overflow": True,
            },
        )

    assert resp.status_code == 200, resp.text
    assert adapter.send_message.await_args.kwargs["target"] == {
        "channel": "CTASK",
        "thread_ts": "100.1",
        "thread_overflow": True,
    }
