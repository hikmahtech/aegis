"""comms asks core again when core could not be asked at boot (#583).

A comms task that started while core was mid-restart fetched no Slack config,
fell back to its (empty) env tokens and served without Slack until someone
restarted it — logged at info, with nothing retrying and nothing raising it.

"Core did not answer" is not "core says Slack is not configured". Only the
second is an idle state. These tests drive `run()` with a fake uvicorn server,
a scripted config fetch and a fake sleep, so no test waits on a real clock.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from functools import partial

from httpx import ASGITransport, AsyncClient

_TOKENS = {
    "configured": True,
    "bot_token": "xoxb-db",
    "app_token": "xapp-db",
    "channel": "slack",
}
_NOT_CONFIGURED = {"configured": False, "bot_token": "", "app_token": "", "channel": ""}


class _FakeServer:
    """uvicorn.Server stand-in: records its app; serve() returns at once."""

    apps: list = []

    def __init__(self, config):
        _FakeServer.apps.append(config.app)

    async def serve(self):
        return


class _Log:
    """Records (level, event) for every log call run() makes."""

    def __init__(self):
        self.events: list[tuple[str, str]] = []

    def info(self, event, **_kw):
        self.events.append(("info", event))

    def warning(self, event, **_kw):
        self.events.append(("warning", event))


def _setup(monkeypatch, answers, *, sleep):
    """Wire run() for a test: no env tokens, a fresh probe state, a fake server
    and logger, a config fetch that returns `answers` in order, and `sleep` in
    place of the retry loop's real sleep. Returns (module, fetch calls, log)."""
    import aegis_comms.__main__ as _main

    for key in ("AEGIS_SLACK_BOT_TOKEN", "AEGIS_SLACK_APP_TOKEN", "AEGIS_CHANNEL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AEGIS_API_KEY", "test-key")
    monkeypatch.setenv("AEGIS_CORE_URL", "http://core:8080")
    monkeypatch.setenv("OTEL_ENABLED", "false")

    monkeypatch.setattr(_main, "_slack_socket_state", _main._SlackSocketState())
    _FakeServer.apps = []
    monkeypatch.setattr(_main.uvicorn, "Server", _FakeServer)
    log = _Log()
    monkeypatch.setattr(_main, "logger", log)

    remaining = list(answers)
    fetches: list[bool] = []

    async def _fetch(settings):
        fetches.append(True)
        return remaining.pop(0) if remaining else None

    monkeypatch.setattr(_main, "_fetch_resolved_slack_config", _fetch)
    monkeypatch.setattr(
        _main,
        "_start_slack_when_configured",
        partial(_main._start_slack_when_configured, sleep=sleep),
    )
    return _main, fetches, log


async def _health(app) -> dict:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/health")
    assert resp.status_code == 200
    return resp.json()


async def _until(predicate, *, ticks: int = 200) -> None:
    for _ in range(ticks):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


async def test_fetch_that_fails_then_succeeds_starts_the_listener(monkeypatch):
    delays: list[float] = []

    async def _sleep(delay):
        delays.append(delay)

    _main, fetches, log = _setup(monkeypatch, [None, None, _TOKENS], sleep=_sleep)

    listener_tokens: list[tuple[str, str]] = []
    stops: list[bool] = []

    async def _listener(self):
        # The adapter was built with no token; it must hold the fetched one now.
        listener_tokens.append((self._client.token, self._settings.slack_app_token))
        await asyncio.Event().wait()

    async def _stop(self):
        stops.append(True)

    monkeypatch.setattr(_main.SlackAdapter, "start_listener", _listener)
    monkeypatch.setattr(_main.SlackAdapter, "stop", _stop)

    task = asyncio.create_task(_main.run())
    try:
        await _until(lambda: listener_tokens)
        assert listener_tokens == [("xoxb-db", "xapp-db")]
        assert len(fetches) == 3  # boot + two retries
        assert delays == [5.0, 10.0]
        assert ("warning", "slack_waiting_for_core") in log.events
        assert ("info", "slack_disabled") not in log.events

        body = await _health(_FakeServer.apps[0])
        assert body["configured"] is True
        assert body["inbound"]["last_error"] is None  # the wait is over
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    assert stops == [True]


async def test_health_reports_slack_down_while_waiting_for_core(monkeypatch):
    parked = asyncio.Event()

    async def _sleep(_delay):
        parked.set()
        await asyncio.Event().wait()  # hold the retry loop here

    _main, _fetches, log = _setup(monkeypatch, [None], sleep=_sleep)

    async def _listener_must_not_run(self):
        raise AssertionError("no listener before core answers")

    async def _stop(self):
        return None

    monkeypatch.setattr(_main.SlackAdapter, "start_listener", _listener_must_not_run)
    monkeypatch.setattr(_main.SlackAdapter, "stop", _stop)

    task = asyncio.create_task(_main.run())
    try:
        await asyncio.wait_for(parked.wait(), timeout=5)
        body = await _health(_FakeServer.apps[0])
        assert body["status"] == "ok"  # the delivery server is up
        assert body["configured"] is False
        inbound = body["inbound"]
        assert inbound["healthy"] is False
        assert "core did not answer" in inbound["last_error"]
        assert log.events[0] == ("warning", "slack_waiting_for_core")
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_core_answering_not_configured_stops_the_retry(monkeypatch):
    delays: list[float] = []

    async def _sleep(delay):
        if len(delays) > 20:
            raise AssertionError("still retrying after core answered")
        delays.append(delay)

    # Six failed asks, then core answers that Slack is not configured.
    _main, fetches, log = _setup(monkeypatch, [None] * 6 + [_NOT_CONFIGURED], sleep=_sleep)

    async def _listener_must_not_run(self):
        raise AssertionError("start_listener() must not run when Slack is unconfigured")

    async def _stop(self):
        return None

    monkeypatch.setattr(_main.SlackAdapter, "start_listener", _listener_must_not_run)
    monkeypatch.setattr(_main.SlackAdapter, "stop", _stop)

    # run() returns only once the fake server has returned AND the retry stopped.
    await asyncio.wait_for(_main.run(), timeout=5)

    assert len(fetches) == 7
    assert delays == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0]  # doubling, capped
    assert ("info", "slack_disabled") in log.events
    # Idle, as if core had answered at boot: no stale "waiting" reason.
    assert _main._slack_socket_state.last_error is None
