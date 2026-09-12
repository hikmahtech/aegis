"""CalibreSyncFlow (#510) — one step, its summary is the run's summary."""

from __future__ import annotations

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.flows.calibre_sync import CalibreSyncConfig, CalibreSyncFlow


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "summary",
    [
        {"status": "not_configured"},
        {"status": "ok", "books": 235, "added": 235, "updated": 0, "unchanged": 0, "failed": 0},
    ],
)
async def test_the_run_reports_what_the_sync_did(summary):
    calls: list[int] = []

    # No parameters: the activity reads the Integrations page itself, so a
    # changed calibre-web login needs no new flow input. A flow that passed
    # one would fail here.
    @activity.defn(name="sync_calibre_library")
    async def stub() -> dict:
        calls.append(activity.info().attempt)
        return summary

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue="tq", workflows=[CalibreSyncFlow], activities=[stub]),
    ):
        result = await env.client.execute_workflow(
            CalibreSyncFlow.run,
            CalibreSyncConfig(agent_id="raphael"),
            id=f"calibre-{summary['status']}",
            task_queue="tq",
        )
    assert result == summary
    assert calls == [1], "one step, run once"


@pytest.mark.asyncio
async def test_a_sync_that_fails_once_is_retried():
    """calibre-web restarting mid-run is a retry, not a failed day."""
    calls: list[int] = []

    @activity.defn(name="sync_calibre_library")
    async def flaky() -> dict:
        calls.append(activity.info().attempt)
        if len(calls) == 1:
            raise RuntimeError("calibre-web is restarting")
        return {"status": "ok", "books": 1}

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue="tq", workflows=[CalibreSyncFlow], activities=[flaky]),
    ):
        result = await env.client.execute_workflow(
            CalibreSyncFlow.run,
            CalibreSyncConfig(agent_id="raphael"),
            id="calibre-retry",
            task_queue="tq",
        )
    assert result == {"status": "ok", "books": 1}
    assert calls == [1, 2]
