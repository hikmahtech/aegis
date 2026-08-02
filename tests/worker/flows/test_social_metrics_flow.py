"""SocialMetricsFlow orchestration — stubbed activities, time-skipping env."""

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


def _stubs(
    refresh_calls: list,
    stuck_calls: list,
    report_calls: list,
    *,
    stuck: list | None = None,
    stuck_raises: bool = False,
):
    @activity.defn(name="refresh_post_metrics")
    async def refresh_post_metrics(
        window_days: int = 14, lookahead_days: int = 45, max_rows: int = 200
    ) -> dict:
        refresh_calls.append((window_days, lookahead_days, max_rows))
        return {"refreshed": 3, "failed": 1}

    @activity.defn(name="find_stuck_posts")
    async def find_stuck_posts(overdue_hours: int = 6, limit: int = 50) -> list[dict]:
        stuck_calls.append((overdue_hours, limit))
        if stuck_raises:
            raise RuntimeError("detector exploded")
        return list(stuck or [])

    @activity.defn(name="report_stuck_posts")
    async def report_stuck_posts(
        findings: list[dict],
        agent_id: str = "sebas",
        dedup_hours: int = 168,
        recovery_hours: int = 720,
    ) -> dict:
        report_calls.append((findings, agent_id, dedup_hours, recovery_hours))
        return {"alerted": len(findings), "deduped": 0, "muted": 0, "recovered": 0}

    return [refresh_post_metrics, find_stuck_posts, report_stuck_posts]


async def _run(temporal_env, config: SocialMetricsConfig, acts):
    tq = f"test-{uuid4().hex[:8]}"
    async with Worker(
        temporal_env.client,
        task_queue=tq,
        workflows=[SocialMetricsFlow],
        activities=acts,
    ):
        return await temporal_env.client.execute_workflow(
            SocialMetricsFlow.run,
            config,
            id=f"social-metrics-{uuid4()}",
            task_queue=tq,
        )


async def test_refresh_then_stuck_check_then_report(temporal_env):
    """Every config knob reaches its activity, and the watchdog's findings are
    handed to the reporter verbatim."""
    refresh, stuck, report = [], [], []
    found = [{"subject": "pz-1", "state": "QUEUE"}, {"subject": "pz-2", "state": "unknown"}]
    result = await _run(
        temporal_env,
        SocialMetricsConfig(
            agent_id="sebas",
            window_days=21,
            lookahead_days=60,
            max_rows=111,
            stuck_after_hours=9,
            max_stuck=7,
            dedup_hours=48,
            recovery_hours=99,
        ),
        _stubs(refresh, stuck, report, stuck=found),
    )
    assert refresh == [(21, 60, 111)]
    assert stuck == [(9, 7)]
    assert report == [(found, "sebas", 48, 99)]
    assert result == {
        "refreshed": 3,
        "failed": 1,
        "stuck": 2,
        "stuck_status": "ok",
        "alerted": 2,
        "deduped": 0,
        "muted": 0,
        "recovered": 0,
    }


async def test_report_still_runs_with_no_findings_so_recovery_can_fire(temporal_env):
    """An empty sweep is exactly when a previously-alerted post gets its
    recovery notice — the reporter must be called, not short-circuited."""
    refresh, stuck, report = [], [], []
    result = await _run(
        temporal_env,
        SocialMetricsConfig(agent_id="sebas"),
        _stubs(refresh, stuck, report, stuck=[]),
    )
    assert report == [([], "sebas", 168, 720)]
    assert result["stuck"] == 0
    assert result["stuck_status"] == "ok"


async def test_detector_failure_degrades_and_skips_reporting(temporal_env):
    """A broken detector must not fail the metrics refresh that already
    succeeded — and must NOT reach the reporter, because an empty findings list
    there is indistinguishable from "everything recovered" and would fire bogus
    recovery notices that also re-arm dedup."""
    refresh, stuck, report = [], [], []
    result = await _run(
        temporal_env,
        SocialMetricsConfig(agent_id="sebas"),
        _stubs(refresh, stuck, report, stuck_raises=True),
    )
    assert result == {"refreshed": 3, "failed": 1, "stuck_status": "check_failed"}
    assert report == []
    assert refresh == [(14, 45, 200)]


async def test_check_stuck_false_is_a_kill_switch(temporal_env):
    """Flipping `activities.config` propagates without a redeploy, so the
    watchdog can be silenced from the DB if it ever gets noisy."""
    refresh, stuck, report = [], [], []
    result = await _run(
        temporal_env,
        SocialMetricsConfig(agent_id="sebas", check_stuck=False),
        _stubs(refresh, stuck, report),
    )
    assert result == {"refreshed": 3, "failed": 1, "stuck_status": "disabled"}
    assert (stuck, report) == ([], [])
