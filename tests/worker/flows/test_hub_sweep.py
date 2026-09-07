"""HubSweepFlow calls the promotion activity and reports the count."""

from __future__ import annotations

import uuid

import pytest
from aegis_worker.flows.hub_sweep import HubSweepConfig, HubSweepFlow
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

_calls: list[int] = []


@activity.defn(name="promote_expired_suppressions")
async def _promote() -> dict:
    _calls.append(1)
    return {"promoted": 2, "problem_ids": ["a", "b"]}


@pytest.mark.asyncio
async def test_sweep_promotes_and_reports():
    _calls.clear()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=f"hub-{uuid.uuid4()}",
            workflows=[HubSweepFlow],
            activities=[_promote],
        ) as worker,
    ):
        out = await env.client.execute_workflow(
            HubSweepFlow.run,
            HubSweepConfig(agent_id="pandoras-actor"),
            id=f"hub-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
    assert out == {"promoted": 2}
    assert _calls == [1]
