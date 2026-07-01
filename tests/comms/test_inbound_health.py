"""Channel-aware /api/health + Slack Socket Mode liveness probe.

Blocker A of the Telegram→Slack cutover: under the Slack channel the health
endpoint must NOT report the (never-run) Telegram probe as `reachable=False`
(that false-alarms the DeliveryWatchdog), and it must expose a real signal for
the Socket Mode inbound connection so a dead socket is monitored.

The health endpoint reads module-global probe state, so tests manipulate
`_slack_socket_state` directly.
"""

from __future__ import annotations

import time

from httpx import ASGITransport, AsyncClient


def _build_app(channel: str, monkeypatch):
    """Build the delivery app for the Slack channel.

    TelegramSettings fields use validation_alias env vars (no populate_by_name),
    so the channel/tokens must be set via the environment, not __init__ kwargs.
    """
    from aegis_comms.__main__ import create_delivery_app
    from aegis_comms.config import TelegramSettings

    monkeypatch.setenv("AEGIS_CHANNEL", channel)
    monkeypatch.setenv("AEGIS_API_KEY", "test-key")
    monkeypatch.setenv("AEGIS_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("AEGIS_SLACK_APP_TOKEN", "xapp-test")
    settings = TelegramSettings(_env_file=None)
    from aegis_comms.adapters.slack import SlackAdapter

    adapter = SlackAdapter(settings)
    return create_delivery_app(adapter, settings)


async def _get_health(app) -> dict:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/health")
    assert resp.status_code == 200
    return resp.json()


# ---------------------------------------------------------------------------
# /api/health — channel awareness
# ---------------------------------------------------------------------------


async def test_slack_health_omits_telegram_api_and_reports_inbound(monkeypatch):
    """Under the slack channel: no telegram_api block (the false-alarm source);
    a generic `inbound` block reports the socket as healthy when fresh."""
    import aegis_comms.__main__ as _main

    state = _main._SlackSocketState()
    state.last_connected_at = time.monotonic()
    state.last_error = None
    monkeypatch.setattr(_main, "_slack_socket_state", state)

    body = await _get_health(_build_app("slack", monkeypatch))

    assert "telegram_api" not in body
    assert body["channel"] == "slack"
    inbound = body["inbound"]
    assert inbound["channel"] == "slack"
    assert inbound["healthy"] is True
    assert inbound["last_ok_seconds_ago"] is not None
    assert inbound["last_error"] is None


async def test_slack_health_unhealthy_when_never_connected(monkeypatch):
    """Slack socket never connected → inbound.healthy False, seconds_ago None."""
    import aegis_comms.__main__ as _main

    monkeypatch.setattr(_main, "_slack_socket_state", _main._SlackSocketState())

    body = await _get_health(_build_app("slack", monkeypatch))

    assert "telegram_api" not in body
    inbound = body["inbound"]
    assert inbound["healthy"] is False
    assert inbound["last_ok_seconds_ago"] is None


async def test_slack_health_unhealthy_when_stale(monkeypatch):
    """A connection older than the stale threshold counts as unhealthy."""
    import aegis_comms.__main__ as _main

    state = _main._SlackSocketState()
    state.last_connected_at = time.monotonic() - (_main._PROBE_STALE_THRESHOLD + 60)
    state.last_error = "socket_not_connected"
    monkeypatch.setattr(_main, "_slack_socket_state", state)

    body = await _get_health(_build_app("slack", monkeypatch))

    inbound = body["inbound"]
    assert inbound["healthy"] is False
    assert inbound["last_error"] == "socket_not_connected"


# ---------------------------------------------------------------------------
# Slack socket probe
# ---------------------------------------------------------------------------


class _FakeAdapter:
    def __init__(self, value):
        self._value = value

    async def is_connected(self):
        if isinstance(self._value, Exception):
            raise self._value
        return self._value


async def test_probe_once_records_connected(monkeypatch):
    import aegis_comms.__main__ as _main

    monkeypatch.setattr(_main, "_slack_socket_state", _main._SlackSocketState())
    await _main._slack_socket_probe_once(_FakeAdapter(True))

    assert _main._slack_socket_state.last_connected_at is not None
    assert _main._slack_socket_state.last_error is None


async def test_probe_once_records_disconnected(monkeypatch):
    import aegis_comms.__main__ as _main

    monkeypatch.setattr(_main, "_slack_socket_state", _main._SlackSocketState())
    await _main._slack_socket_probe_once(_FakeAdapter(False))

    assert _main._slack_socket_state.last_connected_at is None
    assert _main._slack_socket_state.last_error == "socket_not_connected"


async def test_probe_once_handles_exception(monkeypatch):
    import aegis_comms.__main__ as _main

    monkeypatch.setattr(_main, "_slack_socket_state", _main._SlackSocketState())
    # Must not raise.
    await _main._slack_socket_probe_once(_FakeAdapter(RuntimeError("boom")))

    assert _main._slack_socket_state.last_error == "boom"


async def test_probe_once_none_leaves_state_untouched(monkeypatch):
    """is_connected() == None means the listener has not started yet; leave the
    last_connected_at watermark alone rather than flapping it to down."""
    import aegis_comms.__main__ as _main

    state = _main._SlackSocketState()
    state.last_connected_at = 123.0
    monkeypatch.setattr(_main, "_slack_socket_state", state)
    await _main._slack_socket_probe_once(_FakeAdapter(None))

    assert _main._slack_socket_state.last_connected_at == 123.0


# ---------------------------------------------------------------------------
# SlackAdapter.is_connected
# ---------------------------------------------------------------------------


async def test_slack_adapter_is_connected_none_before_listener():
    from aegis_comms.adapters.slack import SlackAdapter
    from aegis_comms.config import TelegramSettings

    adapter = SlackAdapter(
        TelegramSettings(_env_file=None, channel="slack", slack_bot_token="xoxb-x")
    )
    assert await adapter.is_connected() is None


async def test_slack_adapter_is_connected_polls_handler_client():
    from aegis_comms.adapters.slack import SlackAdapter
    from aegis_comms.config import TelegramSettings

    adapter = SlackAdapter(
        TelegramSettings(_env_file=None, channel="slack", slack_bot_token="xoxb-x")
    )

    class _Client:
        async def is_connected(self):
            return True

    class _Handler:
        client = _Client()

    adapter._socket_handler = _Handler()
    assert await adapter.is_connected() is True
