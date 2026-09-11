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
    @activity.defn(name="sync_calibre_library")
    async def stub() -> dict:
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
