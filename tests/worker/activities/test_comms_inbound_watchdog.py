"""HomelabActivities.check_comms_inbound_health — the comms probe.

The alert itself is the delivery watchdog's `comms_inbound_down` problem on
the hub (`flows/delivery_watchdog.py`); this file covers only the probe."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import respx
from aegis_worker.activities.homelab import HomelabActivities
from httpx import Response
from temporalio.testing import ActivityEnvironment

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_act(db_pool=None):
    delivery = AsyncMock()
    delivery.send_message = AsyncMock(return_value={"ok": True})
    return HomelabActivities(db_pool=db_pool, homelab=None, delivery=delivery)


# ---------------------------------------------------------------------------
# check_comms_inbound_health — no-DB tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_health_unreachable_endpoint_returns_unknown():
    """HTTP error reaching the comms service → status='unknown' (backward compat)."""
    respx.get("http://comms.test/api/health").mock(side_effect=Exception("Connection refused"))
    act = _make_act()
    env = ActivityEnvironment()
    result = await env.run(act.check_comms_inbound_health, "http://comms.test")
    assert result == {"status": "unknown"}


@pytest.mark.asyncio
@respx.mock
async def test_health_no_inbound_field_returns_unknown():
    """No `inbound` block in the health body → status='unknown' (do nothing).

    Also covers the removed legacy `telegram_api` fallback: a body carrying
    only that block is treated as unknown now that Telegram is gone.
    """
    respx.get("http://comms.test/api/health").mock(
        return_value=Response(
            200,
            json={
                "status": "ok",
                "service": "aegis-comms",
                "telegram_api": {"reachable": True, "last_ok_seconds_ago": 30},
            },
        )
    )
    act = _make_act()
    env = ActivityEnvironment()
    result = await env.run(act.check_comms_inbound_health, "http://comms.test")
    assert result == {"status": "unknown"}


@pytest.mark.asyncio
@respx.mock
async def test_health_slack_inbound_healthy_returns_ok():
    """Slack body: generic `inbound` block healthy → status='ok'."""
    respx.get("http://comms.test/api/health").mock(
        return_value=Response(
            200,
            json={
                "status": "ok",
                "channel": "slack",
                "inbound": {
                    "channel": "slack",
                    "healthy": True,
                    "last_ok_seconds_ago": 30,
                    "last_error": None,
                },
            },
        )
    )
    act = _make_act()
    env = ActivityEnvironment()
    result = await env.run(act.check_comms_inbound_health, "http://comms.test")
    assert result == {"status": "ok"}


@pytest.mark.asyncio
@respx.mock
async def test_health_slack_inbound_unhealthy_returns_down():
    """Slack body: `inbound.healthy` False → status='down' with the inbound details."""
    respx.get("http://comms.test/api/health").mock(
        return_value=Response(
            200,
            json={
                "status": "ok",
                "channel": "slack",
                "inbound": {
                    "channel": "slack",
                    "healthy": False,
                    "last_ok_seconds_ago": 900,
                    "last_error": "socket_not_connected",
                },
            },
        )
    )
    act = _make_act()
    env = ActivityEnvironment()
    result = await env.run(act.check_comms_inbound_health, "http://comms.test")
    assert result["status"] == "down"
    assert result["last_ok_seconds_ago"] == 900
    assert result["last_error"] == "socket_not_connected"


@pytest.mark.asyncio
async def test_health_empty_url_returns_unknown():
    """No comms_url configured → status='unknown'."""
    act = _make_act()
    env = ActivityEnvironment()
    result = await env.run(act.check_comms_inbound_health, "")
    assert result == {"status": "unknown"}
