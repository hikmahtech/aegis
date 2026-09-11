"""TradingDeskFlow runs exactly one desk_tick and returns what it said."""

from __future__ import annotations

import uuid

from aegis_worker.flows.trading_desk import TradingDeskConfig, TradingDeskFlow
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker


async def test_the_flow_runs_one_desk_tick():
    calls: list[int] = []

    @activity.defn(name="desk_tick")
    async def desk_tick() -> dict:
        calls.append(1)
        return {"day": "2026-09-11", "planned": "orders"}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"tq-{uuid.uuid4()}"
        async with Worker(env.client, task_queue=queue, workflows=[TradingDeskFlow], activities=[desk_tick]):
            out = await env.client.execute_workflow(
                TradingDeskFlow.run, TradingDeskConfig(agent_id="maou"), id=f"desk-{uuid.uuid4()}", task_queue=queue
            )
    assert out == {"day": "2026-09-11", "planned": "orders"}
    assert calls == [1]
