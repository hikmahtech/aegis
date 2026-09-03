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
