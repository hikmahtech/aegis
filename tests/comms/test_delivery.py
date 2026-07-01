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
    from aegis_comms.config import TelegramSettings

    settings = TelegramSettings(
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
