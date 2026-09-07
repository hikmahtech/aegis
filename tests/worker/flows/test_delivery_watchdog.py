"""DeliveryWatchdogFlow — undelivered cards and the comms probe, on the hub."""

from __future__ import annotations

import datetime as dt

import pytest
from aegis_worker.activities.homelab import HomelabActivities
from temporalio import activity, workflow
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.flows.delivery_watchdog import (
        DeliveryWatchdogConfig,
        DeliveryWatchdogFlow,
    )

_find_calls: list[tuple] = []
_notify_calls: list[list] = []
_hub_calls: list[dict] = []
_hub: dict = {"open": set()}  # classes the fake hub currently holds open


def _make_find(rows):
    @activity.defn(name="find_undelivered_interactions")
    async def stub_find(threshold_seconds: int = 120, window_hours: int = 24) -> list[dict]:
        _find_calls.append((threshold_seconds, window_hours))
        return rows

    return stub_find


@activity.defn(name="notify_undelivered_interactions")
async def stub_notify(rows: list[dict]) -> None:
    _notify_calls.append(rows)


def _make_health(health: dict):
    @activity.defn(name="check_comms_inbound_health")
    async def stub_health(comms_url: str) -> dict:
        return health

    return stub_health


@activity.defn(name="reconcile_findings")
async def stub_reconcile(inp: dict) -> dict:
    """A one-problem-per-class hub: a class is fresh when it was not open,
    resolved when it was open and is missing from the findings."""
    _hub_calls.append(inp)
    seen = {f["klass"] for f in inp["findings"]}
    fresh = [
        {**f, "problem_id": f"prob-{f['klass']}"}
        for f in inp["findings"]
        if f["klass"] not in _hub["open"]
    ]
    resolved = [
        {"klass": k, "subject": "x", "problem_id": f"prob-{k}"}
        for k in inp["classes"]
        if k in _hub["open"] and k not in seen
    ]
    _hub["open"] = (_hub["open"] | seen) - {r["klass"] for r in resolved}
    return {"fresh": fresh, "attached": 0, "muted": 0, "suppressed": 0, "resolved": resolved}


def _reset(open_classes: set[str] | None = None) -> None:
    _find_calls.clear()
    _notify_calls.clear()
    _hub_calls.clear()
    _hub["open"] = set(open_classes or set())


async def _run(find_stub, config, wf_id, health_stub=None):
    activities = [find_stub, stub_notify, health_stub or _make_health({"status": "ok"}), stub_reconcile]
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[DeliveryWatchdogFlow],
            activities=activities,
        ),
    ):
        return await env.client.execute_workflow(
            DeliveryWatchdogFlow.run, config, id=wf_id, task_queue="tq"
        )


@pytest.mark.asyncio
async def test_notifies_when_undelivered_found():
    _reset()
    rows = [{"id": "i1", "origin": "alert_confirm_repo", "status": "pending"}]
    result = await _run(_make_find(rows), DeliveryWatchdogConfig(), "dw-1")
    assert result == {"undelivered": 1, "comms_inbound_status": "ok"}
    assert len(_notify_calls) == 1
    assert _find_calls == [(120, 24)]
    finding = _hub_calls[0]["findings"][0]
    assert (finding["klass"], finding["subject"]) == ("undelivered_cards", "interactions")
    assert finding["payload"] == {"count": 1, "by_origin": {"alert_confirm_repo": 1}}
    assert _hub_calls[0]["classes"] == ["undelivered_cards", "comms_inbound_down"]


@pytest.mark.asyncio
async def test_an_open_undelivered_problem_is_not_recarded_every_hour():
    _reset(open_classes={"undelivered_cards"})
    rows = [{"id": "i1", "origin": "alert_confirm_repo", "status": "pending"}]
    result = await _run(_make_find(rows), DeliveryWatchdogConfig(), "dw-1b")
    assert result["undelivered"] == 1
    assert _notify_calls == []


@pytest.mark.asyncio
async def test_silent_when_none_undelivered():
    _reset()
    result = await _run(_make_find([]), DeliveryWatchdogConfig(), "dw-2")
    assert result == {"undelivered": 0, "comms_inbound_status": "ok"}
    assert _notify_calls == []
    assert _hub_calls[0]["findings"] == []


@pytest.mark.asyncio
async def test_records_comms_down_and_alerted():
    """A down comms status must surface in result_summary alongside whether
    the hub raised it — this half of the watchdog was previously invisible
    (issue #120). No chat card: the chat channel is the thing that is down."""
    _reset()
    health = {"status": "down", "last_ok_seconds_ago": 900, "last_error": "socket closed"}
    result = await _run(
        _make_find([]), DeliveryWatchdogConfig(), "dw-3", health_stub=_make_health(health)
    )
    assert result == {
        "undelivered": 0,
        "comms_inbound_status": "down",
        "comms_inbound_alerted": True,
    }
    finding = _hub_calls[0]["findings"][0]
    assert (finding["klass"], finding["subject"], finding["severity"]) == (
        "comms_inbound_down",
        "polling",
        "critical",
    )
    assert "last ok 900s ago" in finding["title"]
    assert finding["payload"] == {"last_ok_seconds_ago": 900, "last_error": "socket closed"}
    assert _notify_calls == []


@pytest.mark.asyncio
async def test_sustained_outage_is_one_problem():
    _reset(open_classes={"comms_inbound_down"})
    health = {"status": "down", "last_ok_seconds_ago": None, "last_error": None}
    result = await _run(
        _make_find([]), DeliveryWatchdogConfig(), "dw-3b", health_stub=_make_health(health)
    )
    assert result["comms_inbound_alerted"] is False
    assert "last ok never" in _hub_calls[0]["findings"][0]["title"]


@pytest.mark.asyncio
async def test_healthy_tick_resolves_the_open_outage():
    """Recovery resolves the problem, which closes its task. Without this the
    task stays open forever."""
    _reset(open_classes={"comms_inbound_down"})
    result = await _run(_make_find([]), DeliveryWatchdogConfig(), "dw-4")
    assert result == {
        "undelivered": 0,
        "comms_inbound_status": "ok",
        "comms_inbound_resolved": True,
    }


@pytest.mark.asyncio
async def test_records_comms_check_failed_without_crashing_watchdog():
    """A failing health check must not fail the whole watchdog run — it
    degrades to a visible status, and says nothing about the comms class to
    the hub (neither reported nor resolved)."""
    _reset(open_classes={"comms_inbound_down"})

    @activity.defn(name="check_comms_inbound_health")
    async def failing_health(comms_url: str) -> dict:
        raise RuntimeError("connection refused")

    result = await _run(_make_find([]), DeliveryWatchdogConfig(), "dw-5", health_stub=failing_health)
    assert result == {"undelivered": 0, "comms_inbound_status": "check_failed"}
    assert _hub_calls[0]["classes"] == ["undelivered_cards"]


@pytest.mark.asyncio
async def test_silent_config_records_but_never_cards():
    _reset()
    rows = [{"id": "i1", "origin": "x", "status": "pending"}]
    result = await _run(_make_find(rows), DeliveryWatchdogConfig(silent=True), "dw-6")
    assert result["undelivered"] == 1
    assert _notify_calls == []
    assert _hub_calls[0]["findings"][0]["klass"] == "undelivered_cards"


# ----- activity (real Postgres) ---------------------------------------------


@pytest.mark.asyncio
async def test_find_undelivered_interactions_query(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM interactions WHERE id IN ($1,$2,$3,$4,$5)",
            "00000000-0000-0000-0000-0000000000a1",
            "00000000-0000-0000-0000-0000000000a2",
            "00000000-0000-0000-0000-0000000000a3",
            "00000000-0000-0000-0000-0000000000a4",
            "00000000-0000-0000-0000-0000000000a5",
        )
        old = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10)
        cols = (
            "(id, flow_run_id, agent_id, kind, origin, prompt, status, "
            "timeout_policy, telegram_message_id, created_at)"
        )
        # Undelivered (NULL telegram_message_id, old enough) → should be found.
        await conn.execute(
            f"INSERT INTO interactions {cols} "
            "VALUES ($1,'r','pandoras-actor','choice','alert_confirm_repo','p','pending','archive',NULL,$2)",
            "00000000-0000-0000-0000-0000000000a1",
            old,
        )
        # Delivered (has telegram_message_id) → must NOT be found.
        await conn.execute(
            f"INSERT INTO interactions {cols} "
            "VALUES ($1,'r','pandoras-actor','choice','alert_confirm_repo','p','pending','archive',12345,$2)",
            "00000000-0000-0000-0000-0000000000a2",
            old,
        )
        # Too recent (within grace) → not yet counted.
        await conn.execute(
            f"INSERT INTO interactions {cols} "
            "VALUES ($1,'r','pandoras-actor','choice','x','p','pending','archive',NULL,now())",
            "00000000-0000-0000-0000-0000000000a3",
        )
        # Resolved but never delivered (NULL telegram_message_id, old enough) →
        # must NOT be found. A terminal-state card is no longer actionable, so
        # it must not re-fire the alert for the whole 24h window (the false
        # alarm this guard fixes: a card force-resolved out-of-band).
        await conn.execute(
            f"INSERT INTO interactions {cols} "
            "VALUES ($1,'r','pandoras-actor','choice','alert_confirm_repo','p','resolved','archive',NULL,$2)",
            "00000000-0000-0000-0000-0000000000a4",
            old,
        )
        # Delivered via Slack: telegram_message_id NULL but delivery_ref set →
        # must NOT be found. Post-cutover cards carry a channel-neutral
        # delivery_ref, not a numeric telegram_message_id.
        await conn.execute(
            "INSERT INTO interactions (id, flow_run_id, agent_id, kind, origin, prompt, "
            "status, timeout_policy, telegram_message_id, delivery_ref, created_at) "
            "VALUES ($1,'r','pandoras-actor','choice','alert_confirm_repo','p','pending',"
            "'archive',NULL,$2,$3)",
            "00000000-0000-0000-0000-0000000000a5",
            {"adapter": "slack", "channel": "C0X", "ts": "1.1"},
            old,
        )
    try:
        act = HomelabActivities(db_pool=db_pool, homelab=None, delivery=None)
        env = ActivityEnvironment()
        rows = await env.run(act.find_undelivered_interactions, 120, 24)
        ids = {r["id"] for r in rows}
        assert "00000000-0000-0000-0000-0000000000a1" in ids
        assert "00000000-0000-0000-0000-0000000000a2" not in ids
        assert "00000000-0000-0000-0000-0000000000a3" not in ids
        # Resolved-but-undelivered must be excluded by the status='pending' guard.
        assert "00000000-0000-0000-0000-0000000000a4" not in ids
        # Slack-delivered (delivery_ref set) must be excluded — channel-neutral.
        assert "00000000-0000-0000-0000-0000000000a5" not in ids
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM interactions WHERE id IN ($1,$2,$3,$4,$5)",
                "00000000-0000-0000-0000-0000000000a1",
                "00000000-0000-0000-0000-0000000000a2",
                "00000000-0000-0000-0000-0000000000a3",
                "00000000-0000-0000-0000-0000000000a4",
                "00000000-0000-0000-0000-0000000000a5",
            )
