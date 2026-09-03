"""CleanupFlow — the step wiring, with every activity stubbed.

The activities have their own tests against real Postgres; what is under test
here is that the flow actually calls the task-session sweep, passes it the
configured window, reports it under its own key, and neither suppresses nor is
suppressed by the steps around it.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from aegis_worker.flows.cleanup import CleanupConfig, CleanupFlow

_calls: dict[str, list] = {}


def _record(name: str, value) -> None:
    _calls.setdefault(name, []).append(value)


@activity.defn(name="cleanup_old_dispatches")
async def _stub_dispatches(days: int) -> dict:
    _record("dispatches", days)
    return {"candidates": 0, "deleted_from_db": 0}


@activity.defn(name="prune_old_records")
async def _stub_prune(config: dict) -> dict:
    _record("prune", config)
    return {"audit_log": 3}


@activity.defn(name="archive_orphan_interactions")
async def _stub_orphans(threshold_days: int) -> dict:
    _record("orphans", threshold_days)
    return {"archived": 1, "threshold_days": threshold_days}


@activity.defn(name="cleanup_task_sessions")
async def _stub_sessions(days: int) -> dict:
    _record("sessions", days)
    return {"removed": 2, "skipped": 1}


@activity.defn(name="prune_old_records")
async def _stub_prune_boom(config: dict) -> dict:
    _record("prune", config)
    raise RuntimeError("relation does not exist")


@activity.defn(name="cleanup_task_sessions")
async def _stub_sessions_boom(days: int) -> dict:
    _record("sessions", days)
    raise RuntimeError("ssh: no route to host")


async def _run(config: CleanupConfig, *activities) -> dict:
    _calls.clear()
    acts = list(activities) or [_stub_dispatches, _stub_prune, _stub_orphans, _stub_sessions]
    tq = f"tq-{uuid4().hex[:8]}"
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue=tq, workflows=[CleanupFlow], activities=acts),
    ):
        return await env.client.execute_workflow(
            CleanupFlow.run,
            config,
            id=f"cleanup-{uuid4().hex[:8]}",
            task_queue=tq,
        )


@pytest.mark.asyncio
async def test_task_session_sweep_runs_and_lands_under_its_own_key():
    result = await _run(CleanupConfig(retentions={"audit_log": 90}, task_session_days=14))

    assert _calls["sessions"] == [14]
    assert result["task_sessions"] == {"removed": 2, "skipped": 1}
    # The neighbouring steps still reported their own results.
    assert result["audit_log"] == 3
    assert result["interactions_archived"] == 1


@pytest.mark.asyncio
async def test_default_window_is_seven_days():
    result = await _run(CleanupConfig(retentions={"audit_log": 90}))

    assert _calls["sessions"] == [7]
    assert result["task_sessions"] == {"removed": 2, "skipped": 1}


@pytest.mark.asyncio
async def test_zero_days_skips_the_sweep_entirely():
    """0 is the operator's off switch — the activity must not run at all, and
    no key is reported, so a disabled sweep is not read as a sweep of nothing."""
    result = await _run(CleanupConfig(retentions={"audit_log": 90}, task_session_days=0))

    assert "sessions" not in _calls
    assert "task_sessions" not in result


@pytest.mark.asyncio
async def test_sweep_failure_is_marked_not_fatal():
    result = await _run(
        CleanupConfig(retentions={"audit_log": 90}),
        _stub_dispatches,
        _stub_prune,
        _stub_orphans,
        _stub_sessions_boom,
    )

    assert result["task_sessions"] == {"status": "failed"}
    # The steps before it still reported — one broken sweep is not a broken run.
    assert result["audit_log"] == 3
    assert result["interactions_archived"] == 1


@pytest.mark.asyncio
async def test_prune_failure_does_not_suppress_the_sweep():
    """Each step is independent: a prune blowing up must not silently stop the
    worktrees being released."""
    result = await _run(
        CleanupConfig(retentions={"audit_log": 90}),
        _stub_dispatches,
        _stub_prune_boom,
        _stub_orphans,
        _stub_sessions,
    )

    assert result["prune_status"] == "failed"
    assert result["task_sessions"] == {"removed": 2, "skipped": 1}
