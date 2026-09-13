"""The alertmanager reconciliation activity, and every way it declines to act.

This is the one place in the hub where getting a read wrong resolves problems
in bulk, so all of it fails closed. The guard worth the most is the uptime one:
the defect this fixes (#551) was caused by alertmanager restarting, and a
freshly restarted alertmanager holds NOTHING until Prometheus re-sends — so
reconciling against that empty set would resolve the whole estate at once.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from aegis_worker.activities.hub import HubActivities, _uptime_since
from httpx import Response
from temporalio.testing import ActivityEnvironment

pytestmark = pytest.mark.asyncio

URL = "http://alertmanager:9093"
STATUS = f"{URL}/api/v2/status"
ALERTS = f"{URL}/api/v2/alerts"
NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def _status_body(started: datetime) -> dict:
    """Alertmanager's `uptime` is a START TIMESTAMP, not a duration — measured
    against the live instance on 2026-09-13, which is why this shape matters."""
    return {"uptime": started.isoformat().replace("+00:00", "Z"), "cluster": {"status": "ready"}}


async def _run(url: str = URL, **kw) -> dict:
    acts = HubActivities(db_pool=AsyncMock())
    return await ActivityEnvironment().run(acts.reconcile_alertmanager, url, kw.get("min_uptime", 900))


def test_uptime_is_parsed_from_a_start_timestamp():
    """Falsifiable: treat the field as a duration and this returns nonsense."""
    started = NOW - timedelta(hours=3)
    assert _uptime_since(started.isoformat().replace("+00:00", "Z"), NOW) == timedelta(hours=3)
    # Naive timestamps are read as UTC rather than crashing.
    assert _uptime_since("2026-09-13T09:00:00", NOW) == timedelta(hours=3)
    # Unreadable is None, which the caller turns into "do not reconcile".
    assert _uptime_since("", NOW) is None
    assert _uptime_since("up 3 hours", NOW) is None


async def test_no_url_does_nothing():
    """A fork ships nobody's monitoring host, so unset means off."""
    out = await _run(url="   ")
    assert out["skipped"] == "not_configured"
    assert out["resolved"] == 0


@respx.mock
async def test_a_freshly_restarted_alertmanager_resolves_nothing():
    """THE guard. A restart is what caused #551 in the first place; reconciling
    against the empty set it holds afterwards would resolve every open problem.

    Falsifiable: remove the uptime check and this returns a reconciliation.
    """
    respx.get(STATUS).mock(return_value=Response(200, json=_status_body(datetime.now(UTC))))
    respx.get(ALERTS).mock(return_value=Response(200, json=[]))

    out = await _run()

    assert out["skipped"] == "alertmanager_just_started"
    assert out["resolved"] == 0


@respx.mock
async def test_an_unreachable_alertmanager_resolves_nothing():
    """An unreachable monitoring stack must never read as "everything
    recovered"."""
    respx.get(STATUS).mock(side_effect=httpx.ConnectError("no route to host"))
    out = await _run()
    assert out["skipped"] == "unreachable"
    assert out["resolved"] == 0


@respx.mock
async def test_a_non_200_resolves_nothing():
    respx.get(STATUS).mock(return_value=Response(503, text="unavailable"))
    out = await _run()
    assert out["skipped"] == "unreachable"


@respx.mock
async def test_an_unreadable_uptime_resolves_nothing():
    respx.get(STATUS).mock(return_value=Response(200, json={"uptime": "ages"}))
    respx.get(ALERTS).mock(return_value=Response(200, json=[]))
    out = await _run()
    assert out["skipped"] == "uptime_unreadable"


@respx.mock
async def test_a_long_running_alertmanager_reconciles_and_counts_silenced_alerts_as_active():
    """A silenced or inhibited alert is still firing — someone has only asked
    not to be told — so it counts as active and its problem stays live.

    Falsifiable: filter on `state == "active"` and the suppressed fingerprint
    stops protecting its problem.
    """
    respx.get(STATUS).mock(
        return_value=Response(200, json=_status_body(datetime.now(UTC) - timedelta(hours=4)))
    )
    respx.get(ALERTS).mock(
        return_value=Response(
            200,
            json=[
                {"fingerprint": "aaa111", "status": {"state": "active"}},
                {"fingerprint": "bbb222", "status": {"state": "suppressed"}},
                {"fingerprint": "", "status": {"state": "active"}},
            ],
        )
    )

    captured: dict = {}

    async def _fake_reconcile(pool, *, active_fingerprints, now=None):
        captured["active"] = active_fingerprints
        return {"checked": 3, "resolved": [{"problem_id": "p-1"}]}

    import aegis_worker.activities.hub as hub_activities

    original = hub_activities.hub_watch.reconcile_alertmanager
    hub_activities.hub_watch.reconcile_alertmanager = _fake_reconcile
    try:
        out = await _run()
    finally:
        hub_activities.hub_watch.reconcile_alertmanager = original

    assert captured["active"] == {"aaa111", "bbb222"}, "a suppressed alert is still firing"
    assert out["resolved"] == 1
    assert out["active_alerts"] == 2
