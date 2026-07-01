"""ServiceDriftFlow wait-and-watch: re-check before notifying.

A single hourly snapshot catches services mid-rollout / mid-restart
(momentarily 0 replicas) and batch jobs that just finished. The flow now
re-checks after a delay and keeps only drifts that are STILL present, so
transient blips never reach Telegram.
"""

from __future__ import annotations

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.flows.service_drift import ServiceDriftConfig, ServiceDriftFlow


# ----- test doubles ---------------------------------------------------------

_collect_calls: list[int] = []
_persist_calls: list[list[dict]] = []
_resolve_calls: list[list[str]] = []
_notify_calls: list[dict] = []


def _service(name: str, desired: int, actual: int) -> dict:
    return {
        "name": name,
        "stack": name.split("_")[0],
        "replicas_desired": desired,
        "replicas_actual": actual,
    }


def _collected(*services: dict) -> dict:
    return {"services": list(services), "ps_map": {}}


def _make_collect(snapshots: list[dict]):
    """Returns a collect_services stub that yields snapshots[0], [1], ... by
    call index (clamping to the last one if called more often)."""

    @activity.defn(name="collect_services")
    async def stub_collect() -> dict:
        idx = len(_collect_calls)
        _collect_calls.append(idx)
        return snapshots[min(idx, len(snapshots) - 1)]

    return stub_collect


@activity.defn(name="persist_drifts")
async def stub_persist(drifts: list[dict]) -> int:
    _persist_calls.append(drifts)
    return len(drifts)


@activity.defn(name="resolve_stale_drifts")
async def stub_resolve(alert_keys_still_open: list[str]) -> int:
    _resolve_calls.append(alert_keys_still_open)
    return 0


@activity.defn(name="notify_drift")
async def stub_notify(payload: dict) -> None:
    _notify_calls.append(payload)


def _reset() -> None:
    _collect_calls.clear()
    _persist_calls.clear()
    _resolve_calls.clear()
    _notify_calls.clear()


async def _run(collect_stub, config: ServiceDriftConfig, wf_id: str) -> dict:
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="tq",
            workflows=[ServiceDriftFlow],
            activities=[collect_stub, stub_persist, stub_resolve, stub_notify],
        ),
    ):
        return await env.client.execute_workflow(
            ServiceDriftFlow.run,
            config,
            id=wf_id,
            task_queue="tq",
        )


# ----- tests ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_drift_is_rechecked_and_not_notified():
    """Service down on first snapshot, healthy on the re-check → suppressed.
    No Telegram card, nothing persisted."""
    _reset()
    snapshots = [
        _collected(_service("monitoring_prometheus", 1, 0)),  # drifted
        _collected(_service("monitoring_prometheus", 1, 1)),  # recovered
    ]
    result = await _run(
        _make_collect(snapshots),
        ServiceDriftConfig(recheck_delay_seconds=120),
        "drift-transient",
    )

    assert len(_collect_calls) == 2, "must re-collect after the delay"
    assert _notify_calls == [], "transient drift must not notify"
    assert _persist_calls == [[]], "transient drift must not be persisted"
    assert result["suppressed"] == 1


@pytest.mark.asyncio
async def test_persistent_drift_survives_recheck_and_notifies():
    """Service down on both snapshots → confirmed → one card, persisted."""
    _reset()
    down = _collected(_service("mysql_mysql", 1, 0))
    result = await _run(
        _make_collect([down, down]),
        ServiceDriftConfig(recheck_delay_seconds=120),
        "drift-persistent",
    )

    assert len(_collect_calls) == 2
    assert len(_notify_calls) == 1
    assert _notify_calls[0]["service_name"] == "mysql_mysql"
    assert _persist_calls and _persist_calls[0][0]["service_name"] == "mysql_mysql"
    assert result["suppressed"] == 0


@pytest.mark.asyncio
async def test_no_drift_skips_recheck_entirely():
    """All healthy on first snapshot → no second collect, no notify."""
    _reset()
    healthy = _collected(_service("aegis_core", 1, 1))
    result = await _run(
        _make_collect([healthy]),
        ServiceDriftConfig(recheck_delay_seconds=120),
        "drift-healthy",
    )

    assert len(_collect_calls) == 1, "no drift → must not re-collect"
    assert _notify_calls == []
    assert result["suppressed"] == 0


@pytest.mark.asyncio
async def test_recheck_disabled_when_delay_zero():
    """recheck_delay_seconds=0 disables wait-and-watch: single snapshot, the
    drift notifies immediately (escape hatch / legacy behaviour)."""
    _reset()
    down = _collected(_service("rabbitmq_rabbitmq", 1, 0))
    await _run(
        _make_collect([down]),
        ServiceDriftConfig(recheck_delay_seconds=0),
        "drift-no-recheck",
    )

    assert len(_collect_calls) == 1, "delay=0 must not re-collect"
    assert len(_notify_calls) == 1
