"""SocialMetricsFlow orchestration — stubbed activity, time-skipping env."""

from __future__ import annotations

from uuid import uuid4

import pytest_asyncio
from aegis_worker.flows.social_metrics import SocialMetricsConfig, SocialMetricsFlow
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker


@pytest_asyncio.fixture(loop_scope="function")
async def temporal_env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


async def test_refresh_post_metrics_result_passed_through(temporal_env):
    calls: list[int] = []

    @activity.defn(name="refresh_post_metrics")
    async def refresh_post_metrics(window_days: int = 14) -> dict:
        calls.append(window_days)
        return {"refreshed": 3, "failed": 1}

    tq = f"test-{uuid4().hex[:8]}"
    async with Worker(
        temporal_env.client,
        task_queue=tq,
        workflows=[SocialMetricsFlow],
        activities=[refresh_post_metrics],
    ):
        result = await temporal_env.client.execute_workflow(
            SocialMetricsFlow.run,
            SocialMetricsConfig(agent_id="sebas", window_days=21),
            id=f"social-metrics-{uuid4()}",
            task_queue=tq,
        )
        assert result == {"refreshed": 3, "failed": 1}
        assert calls == [21]
