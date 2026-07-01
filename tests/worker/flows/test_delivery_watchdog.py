"""DeliveryWatchdogFlow — surface silently-undelivered interaction cards."""

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


def _make_find(rows):
    @activity.defn(name="find_undelivered_interactions")
    async def stub_find(threshold_seconds: int = 120, window_hours: int = 24) -> list[dict]:
        _find_calls.append((threshold_seconds, window_hours))
        return rows

    return stub_find


@activity.defn(name="notify_undelivered_interactions")
async def stub_notify(rows: list[dict]) -> None:
    _notify_calls.append(rows)


async def _run(find_stub, config, wf_id):
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[DeliveryWatchdogFlow],
            activities=[find_stub, stub_notify],
        ),
    ):
        return await env.client.execute_workflow(
            DeliveryWatchdogFlow.run, config, id=wf_id, task_queue="tq"
        )


@pytest.mark.asyncio
async def test_notifies_when_undelivered_found():
    _find_calls.clear()
    _notify_calls.clear()
    rows = [{"id": "i1", "origin": "alert_confirm_repo", "status": "pending"}]
    result = await _run(_make_find(rows), DeliveryWatchdogConfig(), "dw-1")
    assert result == {"undelivered": 1}
    assert len(_notify_calls) == 1
    assert _find_calls == [(120, 24)]


@pytest.mark.asyncio
async def test_silent_when_none_undelivered():
    _find_calls.clear()
    _notify_calls.clear()
    result = await _run(_make_find([]), DeliveryWatchdogConfig(), "dw-2")
    assert result == {"undelivered": 0}
    assert _notify_calls == []


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
