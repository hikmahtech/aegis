"""Tests for the connector-health tracker (issue #76).

record_connector_health persists a per-connector consecutive-failure counter
in the settings table and posts a Slack system event via comms when the
threshold is crossed, plus a recovery event when the connector comes back.
It must never raise into the caller.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
import respx
from aegis.services.connector_health import record_connector_health

_CONNECTOR = "healthtest"
_KEY = f"connector_health:{_CONNECTOR}"


def _settings(comms_url="http://comms:8081", api_key="test-key"):
    return type("S", (), {"comms_url": comms_url, "api_key": api_key})()


async def _state(db_pool):
    return await db_pool.fetchval("SELECT value FROM settings WHERE key = $1", _KEY)


@pytest_asyncio.fixture(loop_scope="function", autouse=True)
async def _clean_state(db_pool):
    await db_pool.execute("DELETE FROM settings WHERE key = $1", _KEY)
    yield


@pytest.mark.asyncio
@respx.mock
async def test_below_threshold_no_alert(db_pool):
    """Failures under the threshold persist state but never hit comms."""
    for _ in range(2):
        await record_connector_health(
            db_pool, _settings(), _CONNECTOR, ok=False, error="boom", threshold=3
        )
    state = await _state(db_pool)
    assert state["consecutive_failures"] == 2
    assert state["alerted"] is False
    assert state["last_error"] == "boom"


@pytest.mark.asyncio
@respx.mock
async def test_threshold_crossing_alerts_once(db_pool):
    deliver = respx.post("http://comms:8081/api/deliver/message").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    for _ in range(3):
        await record_connector_health(
            db_pool, _settings(), _CONNECTOR, ok=False, error="401 nope", threshold=2
        )
    # Alert fired exactly once (on the 2nd failure), not again on the 3rd.
    assert deliver.call_count == 1
    import json

    body = json.loads(deliver.calls.last.request.content)
    assert body["system_event"] is True
    assert _CONNECTOR in body["text"] and "401 nope" in body["text"]
    assert deliver.calls.last.request.headers["x-api-key"] == "test-key"
    state = await _state(db_pool)
    assert state["consecutive_failures"] == 3
    assert state["alerted"] is True


@pytest.mark.asyncio
@respx.mock
async def test_recovery_sends_event_and_resets(db_pool):
    deliver = respx.post("http://comms:8081/api/deliver/message").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    await record_connector_health(
        db_pool, _settings(), _CONNECTOR, ok=False, error="down", threshold=1
    )
    await record_connector_health(db_pool, _settings(), _CONNECTOR, ok=True)
    assert deliver.call_count == 2
    import json

    body = json.loads(deliver.calls.last.request.content)
    assert "recovered" in body["text"]
    state = await _state(db_pool)
    # `down` is what the hub sweep reads (#571) — the threshold is a per-call
    # argument, so a bare count cannot say whether a connector is past its own.
    assert state == {"consecutive_failures": 0, "alerted": False, "down": False}


@pytest.mark.asyncio
@respx.mock
async def test_success_steady_state_writes_nothing(db_pool):
    """A healthy run with no failure history stays read-only — no settings row."""
    await record_connector_health(db_pool, _settings(), _CONNECTOR, ok=True)
    assert await _state(db_pool) is None


@pytest.mark.asyncio
@respx.mock
async def test_failed_alert_send_retries_on_next_failure(db_pool):
    """A comms outage must not latch `alerted` — the next failure retries."""
    deliver = respx.post("http://comms:8081/api/deliver/message").mock(
        return_value=httpx.Response(500)
    )
    await record_connector_health(
        db_pool, _settings(), _CONNECTOR, ok=False, error="down", threshold=1
    )
    state = await _state(db_pool)
    assert state["alerted"] is False
    await record_connector_health(
        db_pool, _settings(), _CONNECTOR, ok=False, error="down", threshold=1
    )
    assert deliver.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_no_comms_url_never_raises(db_pool):
    """Without comms_url the failure is recorded but no event is attempted."""
    await record_connector_health(
        db_pool, _settings(comms_url=""), _CONNECTOR, ok=False, error="down", threshold=1
    )
    state = await _state(db_pool)
    assert state["consecutive_failures"] == 1
    assert state["alerted"] is False


@pytest.mark.asyncio
async def test_broken_pool_never_raises():
    """DB errors are swallowed — health tracking must not break the caller."""

    class _BadPool:
        async def fetchrow(self, *a):
            raise RuntimeError("db down")

    await record_connector_health(
        _BadPool(), _settings(), _CONNECTOR, ok=False, error="x", threshold=1
    )


# --- #573: a failed ping says what failed -------------------------------------


def _send_failures(logs):
    return [e for e in logs if e.get("event") == "connector_health_event_send_failed"]


@pytest.mark.asyncio
@respx.mock
async def test_a_timeout_is_logged_by_its_type(db_pool):
    """httpx raises its timeouts with no message, so the warning said `error=`
    and nothing else in production. It must name the exception."""
    from structlog.testing import capture_logs

    respx.post("http://comms:8081/api/deliver/message").mock(
        side_effect=httpx.ReadTimeout("")
    )
    with capture_logs() as logs:
        await record_connector_health(
            db_pool, _settings(), _CONNECTOR, ok=False, error="down", threshold=1
        )
    [warning] = _send_failures(logs)
    assert warning["error"] == "ReadTimeout"
    assert (await _state(db_pool))["alerted"] is False


@pytest.mark.asyncio
@respx.mock
async def test_a_refused_reply_is_logged_not_silent(db_pool):
    """A 401 (say, a missing API key) used to return False with no log line at
    all — a quieter failure than the empty error."""
    from structlog.testing import capture_logs

    respx.post("http://comms:8081/api/deliver/message").mock(
        return_value=httpx.Response(401, json={"detail": "Invalid API key"})
    )
    with capture_logs() as logs:
        await record_connector_health(
            db_pool, _settings(), _CONNECTOR, ok=False, error="down", threshold=1
        )
    [warning] = _send_failures(logs)
    assert warning["error"] == "comms answered HTTP 401"
    assert "Invalid API key" in warning["body"]
    assert (await _state(db_pool))["alerted"] is False


@pytest.mark.asyncio
@respx.mock
async def test_an_ok_false_reply_is_logged_and_does_not_latch(db_pool):
    from structlog.testing import capture_logs

    respx.post("http://comms:8081/api/deliver/message").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "not_in_channel"})
    )
    with capture_logs() as logs:
        await record_connector_health(
            db_pool, _settings(), _CONNECTOR, ok=False, error="down", threshold=1
        )
    [warning] = _send_failures(logs)
    assert warning["error"] == "comms answered ok=false"
    assert "not_in_channel" in warning["body"]
    assert (await _state(db_pool))["alerted"] is False


@pytest.mark.asyncio
@respx.mock
async def test_the_send_allows_the_delivery_clients_timeout(db_pool):
    """Comms posts to Slack and logs the dispatch before it answers, so the ping
    gets the worker's own delivery bounds (30 s, 5 s to connect), not 10 s."""
    seen = {}

    def _reply(request):
        seen.update(request.extensions["timeout"])
        return httpx.Response(200, json={"ok": True})

    respx.post("http://comms:8081/api/deliver/message").mock(side_effect=_reply)
    await record_connector_health(
        db_pool, _settings(), _CONNECTOR, ok=False, error="down", threshold=1
    )
    assert seen["read"] == 30.0 and seen["connect"] == 5.0
    assert (await _state(db_pool))["alerted"] is True
