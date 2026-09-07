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
    _calls.append("promote")
    return {"promoted": 2, "problem_ids": ["a", "b"]}


@activity.defn(name="project_pending")
async def _project() -> dict:
    _calls.append("project")
    return {"projected": 3, "created": 1, "errors": 0}


@pytest.mark.asyncio
async def test_sweep_promotes_then_projects_and_reports():
    _calls.clear()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=f"hub-{uuid.uuid4()}",
            workflows=[HubSweepFlow],
            activities=[_promote, _project],
        ) as worker,
    ):
        out = await env.client.execute_workflow(
            HubSweepFlow.run,
            HubSweepConfig(agent_id="pandoras-actor"),
            id=f"hub-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
    assert out == {"promoted": 2, "projected": 3, "created": 1, "errors": 0}
    # promotion first, so a just-promoted problem gets its task in the same tick
    assert _calls == ["promote", "project"]
