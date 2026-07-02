"""Tests: _startup_error helper — channel-aware boot validation (FIX 2).

_startup_error is a pure helper that returns None when the channel is ready
to boot, or an error string when it is not.  It replaces the old flat
`if not telegram_bot_token: return` guard in run() and makes startup channel-aware.

Note: TelegramSettings uses pydantic-settings AliasChoices; field values must be
supplied via environment variables (monkeypatch.setenv) rather than constructor
kwargs, which are ignored for aliased fields.
"""

from __future__ import annotations


def _settings(monkeypatch, **env_vars):
    """Build a TelegramSettings using env vars (the only reliable way with pydantic-settings)."""
    # Clear Slack tokens by default so tests are isolated
    monkeypatch.delenv("AEGIS_SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("AEGIS_SLACK_APP_TOKEN", raising=False)
    monkeypatch.delenv("AEGIS_CHANNEL", raising=False)
    for k, v in env_vars.items():
        monkeypatch.setenv(k, v)
    from aegis_comms.config import TelegramSettings

    return TelegramSettings(_env_file=None)


# ---------------------------------------------------------------------------
# Slack channel
# ---------------------------------------------------------------------------


def test_startup_error_slack_with_both_tokens_is_none(monkeypatch):
    """slack + bot_token + app_token → no error (ready to boot)."""
    from aegis_comms.__main__ import _startup_error

    settings = _settings(
        monkeypatch,
        AEGIS_CHANNEL="slack",
        AEGIS_SLACK_BOT_TOKEN="xoxb-test",
        AEGIS_SLACK_APP_TOKEN="xapp-test",
    )
    assert _startup_error(settings) is None


def test_startup_error_slack_missing_tokens_returns_error(monkeypatch):
    """slack + missing tokens → error string."""
    from aegis_comms.__main__ import _startup_error

    settings = _settings(monkeypatch, AEGIS_CHANNEL="slack")
    err = _startup_error(settings)
    assert err is not None
    assert "slack" in err.lower() or "token" in err.lower()


def test_startup_error_slack_missing_app_token_returns_error(monkeypatch):
    """slack + bot_token present but app_token missing → error (need both)."""
    from aegis_comms.__main__ import _startup_error

    settings = _settings(monkeypatch, AEGIS_CHANNEL="slack", AEGIS_SLACK_BOT_TOKEN="xoxb-test")
    err = _startup_error(settings)
    assert err is not None


# ---------------------------------------------------------------------------
# run() constructs a SlackAdapter directly
# ---------------------------------------------------------------------------


def test_slack_adapter_constructs_from_settings(monkeypatch):
    """run() constructs SlackAdapter(settings) directly (the sole channel)."""
    from aegis_comms.adapters.slack import SlackAdapter

    settings = _settings(
        monkeypatch,
        AEGIS_CHANNEL="slack",
        AEGIS_SLACK_BOT_TOKEN="xoxb-test",
        AEGIS_SLACK_APP_TOKEN="xapp-test",
    )
    adapter = SlackAdapter(settings)
    assert adapter.name == "slack"


# ---------------------------------------------------------------------------
# run() resilience — must boot the delivery app with or without Slack (BUG FIX)
#
# run() used to call `_startup_error(settings)` and `return` before building
# the FastAPI delivery app when Slack tokens were missing, so the process
# exited clean (code 0) with NO /api/health and NO /api/deliver/* while
# unconfigured. run() must now always build+serve the delivery app; Slack
# Socket Mode (start_listener + the liveness probe) is the only thing gated
# on configuration.
# ---------------------------------------------------------------------------


class _FakeUvicornServer:
    """Stand-in for uvicorn.Server: captures the app it was built with and
    `serve()` returns immediately (as if uvicorn shut down cleanly), instead
    of actually binding a socket and blocking forever."""

    captured_apps: list = []

    def __init__(self, config):
        self.config = config
        _FakeUvicornServer.captured_apps.append(config.app)

    async def serve(self):
        return


async def test_run_boots_delivery_app_when_slack_unconfigured(monkeypatch):
    """Tokens ABSENT + core unreachable (DB fetch fails) -> run() must still
    build the delivery app and reach `server.serve()` (never return early),
    must NOT start the Slack listener, and /api/health must report
    configured=False."""
    import aegis_comms.__main__ as _main

    for key in (
        "AEGIS_SLACK_BOT_TOKEN",
        "AEGIS_SLACK_APP_TOKEN",
        "AEGIS_SLACK_SIGNING_SECRET",
        "AEGIS_CHANNEL",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AEGIS_API_KEY", "test-key")
    monkeypatch.setenv("OTEL_ENABLED", "false")

    async def _fetch_fails(settings):
        return None  # core unreachable -> pure env fallback (still empty)

    monkeypatch.setattr(_main, "_fetch_resolved_slack_config", _fetch_fails)

    _FakeUvicornServer.captured_apps = []
    monkeypatch.setattr(_main.uvicorn, "Server", _FakeUvicornServer)

    async def _listener_must_not_run(self):
        raise AssertionError("start_listener() must not run when Slack is unconfigured")

    monkeypatch.setattr(_main.SlackAdapter, "start_listener", _listener_must_not_run)

    await _main.run()  # must complete without raising / without exiting early

    assert len(_FakeUvicornServer.captured_apps) == 1
    app = _FakeUvicornServer.captured_apps[0]

    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is False
    assert body["status"] == "ok"


async def test_run_starts_slack_listener_when_configured(monkeypatch):
    """Tokens PRESENT -> run() builds the delivery app AND starts the Slack
    Socket Mode listener + liveness probe (existing configured-path
    behavior must be unchanged).

    In this branch run() awaits asyncio.gather(start_listener(), serve(),
    probe()) — the probe loops forever by design (same as real production,
    where the process is stopped by external cancellation, not a clean
    return). So drive run() as a background task, let it reach the gather,
    then cancel — mirroring how the real process is shut down.
    """
    import asyncio
    from contextlib import suppress

    import aegis_comms.__main__ as _main

    monkeypatch.setenv("AEGIS_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("AEGIS_SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("AEGIS_API_KEY", "test-key")
    monkeypatch.setenv("OTEL_ENABLED", "false")

    async def _fetch_fails(settings):
        return None  # core unreachable -> falls back to the env tokens above

    monkeypatch.setattr(_main, "_fetch_resolved_slack_config", _fetch_fails)

    _FakeUvicornServer.captured_apps = []
    monkeypatch.setattr(_main.uvicorn, "Server", _FakeUvicornServer)

    listener_calls = []
    stop_calls = []

    async def _fake_listener(self):
        listener_calls.append(True)
        await asyncio.Event().wait()  # blocks until cancelled, like the real Socket Mode handler

    async def _fake_stop(self):
        stop_calls.append(True)

    monkeypatch.setattr(_main.SlackAdapter, "start_listener", _fake_listener)
    monkeypatch.setattr(_main.SlackAdapter, "stop", _fake_stop)

    task = asyncio.create_task(_main.run())
    try:
        for _ in range(50):
            if listener_calls and _FakeUvicornServer.captured_apps:
                break
            await asyncio.sleep(0)
        assert listener_calls == [True]
        assert len(_FakeUvicornServer.captured_apps) == 1

        from httpx import ASGITransport, AsyncClient

        transport = ASGITransport(app=_FakeUvicornServer.captured_apps[0])
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/health")
        body = resp.json()
        assert body["configured"] is True
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        assert stop_calls == [True]  # `finally` in run() still runs adapter.stop() on cancellation


async def test_merge_slack_config_db_wins_over_env(monkeypatch):
    """DB value wins when present; env-sourced settings are the fallback.

    TelegramSettings' fields use validation_alias env vars — constructor
    kwargs are silently ignored for them (see the module docstring above),
    so the env-sourced starting point must come from monkeypatch.setenv,
    same as `_settings()`.
    """
    from aegis_comms.__main__ import _merge_slack_config

    settings = _settings(
        monkeypatch,
        AEGIS_SLACK_BOT_TOKEN="env-bot",
        AEGIS_SLACK_APP_TOKEN="env-app",
        AEGIS_SLACK_SIGNING_SECRET="env-secret",
        AEGIS_CHANNEL="slack",
    )
    assert settings.slack_bot_token == "env-bot"  # sanity: env fallback actually took

    _merge_slack_config(
        settings,
        {
            "configured": True,
            "bot_token": "db-bot",
            "app_token": "db-app",
            "signing_secret": "db-secret",
            "channel": "db-channel",
        },
    )
    assert settings.slack_bot_token == "db-bot"
    assert settings.slack_app_token == "db-app"
    assert settings.slack_signing_secret == "db-secret"
    assert settings.channel == "db-channel"


async def test_merge_slack_config_falls_back_to_env_when_db_empty(monkeypatch):
    """DB fields empty/missing -> the env-sourced settings values are kept."""
    from aegis_comms.__main__ import _merge_slack_config

    settings = _settings(
        monkeypatch, AEGIS_SLACK_BOT_TOKEN="env-bot", AEGIS_SLACK_APP_TOKEN="env-app"
    )
    _merge_slack_config(settings, {"configured": False, "bot_token": "", "app_token": ""})
    assert settings.slack_bot_token == "env-bot"
    assert settings.slack_app_token == "env-app"


async def test_merge_slack_config_noop_on_none(monkeypatch):
    """A None db_config (fetch failed) must not touch settings at all."""
    from aegis_comms.__main__ import _merge_slack_config

    settings = _settings(monkeypatch, AEGIS_SLACK_BOT_TOKEN="env-bot")
    _merge_slack_config(settings, None)
    assert settings.slack_bot_token == "env-bot"


async def test_fetch_resolved_slack_config_returns_none_on_http_error(monkeypatch):
    """core unreachable/erroring -> _fetch_resolved_slack_config returns None
    (never raises), so run() falls back to env config."""
    import httpx
    from aegis_comms.__main__ import _fetch_resolved_slack_config

    class _FailingClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", _FailingClient)

    settings = _settings(monkeypatch, AEGIS_CORE_URL="http://core-down:8080")
    result = await _fetch_resolved_slack_config(settings)
    assert result is None


async def test_fetch_resolved_slack_config_returns_json_on_success(monkeypatch):
    """core reachable -> the resolved JSON body is returned as-is."""
    import httpx
    from aegis_comms.__main__ import _fetch_resolved_slack_config

    payload = {
        "configured": True,
        "bot_token": "db-bot",
        "app_token": "db-app",
        "signing_secret": "db-secret",
        "channel": "slack",
    }

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            assert url == "http://core:8080/api/internal/slack-config"
            assert headers == {"X-API-Key": "test-key"}
            return _FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    settings = _settings(monkeypatch, AEGIS_CORE_URL="http://core:8080", AEGIS_API_KEY="test-key")
    result = await _fetch_resolved_slack_config(settings)
    assert result == payload
