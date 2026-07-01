"""MoneyHygieneDailyFlow — merged daily cancellation + renewal sweep."""

from __future__ import annotations

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.flows.money_hygiene import MoneyHygieneConfig, MoneyHygieneDailyFlow


_detect_calls: list[float] = []
_renewal_calls: list[list] = []
_capture_calls: list[tuple] = []
_notify_cancel_calls: list[dict] = []
_notify_renewal_calls: list[dict] = []

_CANCELLED_ROWS = [
    {"id": "c1", "vendor_name": "Netflix", "amount_cents": 1299, "currency": "USD",
     "cadence": "monthly", "last_seen_at": "2026-03-01 00:00:00"},
    {"id": "c2", "vendor_name": "Spotify", "amount_cents": 999, "currency": "USD",
     "cadence": "monthly", "last_seen_at": "2026-02-15 00:00:00"},
]
_RENEWAL_ROWS = [
    {"charge_id": "r1", "vendor_name": "Domain", "amount_cents": 1500, "currency": "USD",
     "days_left": 7, "next_due_at": "2026-07-01", "account": "personal"},
]


@activity.defn(name="detect_cancellations")
async def stub_detect(threshold_multiplier: float) -> list[dict]:
    _detect_calls.append(threshold_multiplier)
    return list(_CANCELLED_ROWS)


@activity.defn(name="evaluate_renewal_alerts")
async def stub_renewals(thresholds_days: list) -> list[dict]:
    _renewal_calls.append(thresholds_days)
    return list(_RENEWAL_ROWS)


@activity.defn(name="capture_to_inbox")
async def stub_capture(source_tag: str, external_id: str, title: str, description=None) -> str:
    _capture_calls.append((source_tag, external_id, title))
    return f"task-{external_id}"


@activity.defn(name="notify_cancellation")
async def stub_notify_cancel(cancellation: dict) -> None:
    _notify_cancel_calls.append(cancellation)


@activity.defn(name="notify_renewal_alert")
async def stub_notify_renewal(alert: dict) -> None:
    _notify_renewal_calls.append(alert)


ALL_STUBS = [stub_detect, stub_renewals, stub_capture, stub_notify_cancel, stub_notify_renewal]


def _clear() -> None:
    for lst in (_detect_calls, _renewal_calls, _capture_calls,
                _notify_cancel_calls, _notify_renewal_calls):
        lst.clear()


async def _run(config: MoneyHygieneConfig, activities=ALL_STUBS, wid="mh") -> dict:
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue="tq", workflows=[MoneyHygieneDailyFlow], activities=activities),
    ):
        return await env.client.execute_workflow(
            MoneyHygieneDailyFlow.run, config, id=wid, task_queue="tq"
        )


@pytest.mark.asyncio
async def test_runs_both_sweeps_with_config():
    _clear()
    result = await _run(
        MoneyHygieneConfig(threshold_multiplier=3.5, thresholds_days=[14, 7]), wid="mh-1"
    )
    assert result == {"cancelled": 2, "renewals": 1}
    assert _detect_calls == [3.5]
    assert _renewal_calls == [[14, 7]]
    # both sweeps capture + notify
    assert len(_capture_calls) == 3  # 2 cancel + 1 renewal
    assert len(_notify_cancel_calls) == 2
    assert len(_notify_renewal_calls) == 1
    ext_ids = {c[1] for c in _capture_calls}
    assert "cancel-c1" in ext_ids and "renewal-r1-2026-07-01" in ext_ids


@pytest.mark.asyncio
async def test_silent_suppresses_capture_and_notify():
    _clear()
    result = await _run(MoneyHygieneConfig(silent=True), wid="mh-2")
    # DB-state sweeps still run + count, but no user-facing output
    assert result == {"cancelled": 2, "renewals": 1}
    assert _capture_calls == []
    assert _notify_cancel_calls == []
    assert _notify_renewal_calls == []


@pytest.mark.asyncio
async def test_cancellation_failure_does_not_block_renewals():
    """A failing cancellation sweep is isolated; renewals still run."""
    _clear()

    @activity.defn(name="detect_cancellations")
    async def boom(threshold_multiplier: float) -> list[dict]:
        raise RuntimeError("detect down")

    result = await _run(
        MoneyHygieneConfig(),
        activities=[boom, stub_renewals, stub_capture, stub_notify_cancel, stub_notify_renewal],
        wid="mh-3",
    )
    assert result == {"cancelled": 0, "renewals": 1}
    assert len(_notify_renewal_calls) == 1
