"""CleanupFlow — the step wiring, with every activity stubbed.

The activities have their own tests against real Postgres; what is under test
here is that each sweep runs with its configured window, reports under its own
key, and neither suppresses nor is suppressed by the steps around it.

The coding sessions' worktree sweep left with the coding lane (the Development
vertical). A run recorded before that still replays, through the legacy
branch behind `PATCH_DROP_WORK_SESSIONS`.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

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


@activity.defn(name="close_resolved_problems")
async def _stub_close_problems(days: float) -> dict:
    _record("close_problems", days)
    return {"closed": 3, "problem_ids": ["a", "b", "c"]}


@activity.defn(name="close_resolved_problems")
async def _stub_close_problems_boom(days: float) -> dict:
    _record("close_problems", days)
    raise RuntimeError("the hub is unreachable")


@activity.defn(name="cleanup_work_sessions")
async def _stub_sessions(days: int) -> dict:
    _record("sessions", days)
    return {"removed": 2, "skipped": 1}


@activity.defn(name="prune_old_records")
async def _stub_prune_boom(config: dict) -> dict:
    _record("prune", config)
    raise RuntimeError("relation does not exist")


async def _run_with_history(config: CleanupConfig, *activities, flow=CleanupFlow):
    _calls.clear()
    acts = list(activities) or [
        _stub_dispatches,
        _stub_prune,
        _stub_orphans,
        _stub_sessions,
        _stub_close_problems,
    ]
    tq = f"tq-{uuid4().hex[:8]}"
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue=tq, workflows=[flow], activities=acts),
    ):
        handle = await env.client.start_workflow(
            flow.run,
            config,
            id=f"cleanup-{uuid4().hex[:8]}",
            task_queue=tq,
        )
        result = await handle.result()
        return result, await handle.fetch_history()


async def _run(config: CleanupConfig, *activities) -> dict:
    result, _ = await _run_with_history(config, *activities)
    return result


_SHORT = timedelta(seconds=30)


@workflow.defn(name="CleanupFlow")
class _CleanupBeforeTheSessionSweepWent:
    """CleanupFlow as the worker before the coding lane left ran it: the
    dispatch prune, the retention prune, the orphan sweep, the coding
    sessions' worktree sweep, then the problem close sweep. Every run in
    flight at the deploy has this history."""

    @workflow.run
    async def run(self, config: CleanupConfig) -> dict:
        await workflow.execute_activity(
            "cleanup_old_dispatches", args=[30], start_to_close_timeout=_SHORT
        )
        await workflow.execute_activity(
            "prune_old_records", args=[{"retentions": {"audit_log": 90}}], start_to_close_timeout=_SHORT
        )
        await workflow.execute_activity(
            "archive_orphan_interactions", args=[7], start_to_close_timeout=_SHORT
        )
        await workflow.execute_activity(
            "cleanup_work_sessions", args=[7], start_to_close_timeout=_SHORT
        )
        await workflow.execute_activity(
            "close_resolved_problems", args=[7.0], start_to_close_timeout=_SHORT
        )
        return {}


@pytest.mark.asyncio
async def test_a_new_run_no_longer_sweeps_coding_sessions():
    result, history = await _run_with_history(CleanupConfig(retentions={"audit_log": 90}))

    assert "sessions" not in _calls
    assert "work_sessions" not in result
    # The neighbouring steps still reported their own results.
    assert result["audit_log"] == 3
    assert result["interactions_archived"] == 1
    markers = [
        e.marker_recorded_event_attributes.marker_name
        for e in history.events
        if e.HasField("marker_recorded_event_attributes")
    ]
    assert markers, "a new run records the PATCH_DROP_WORK_SESSIONS marker"
    await Replayer(workflows=[CleanupFlow]).replay_workflow(history)


@pytest.mark.asyncio
async def test_a_run_that_swept_sessions_replays_after_the_step_went():
    """Falsifiable: delete the `if not workflow.patched(PATCH_DROP_WORK_SESSIONS)`
    branch from `cleanup.py` and this fails with a nondeterminism error (the
    history schedules `cleanup_work_sessions`, the new shape never does)."""
    _, history = await _run_with_history(
        CleanupConfig(retentions={"audit_log": 90}),
        _stub_dispatches,
        _stub_prune,
        _stub_orphans,
        _stub_sessions,
        _stub_close_problems,
        flow=_CleanupBeforeTheSessionSweepWent,
    )
    assert _calls["sessions"] == [7]  # the premise: the old shape really ran it
    await Replayer(workflows=[CleanupFlow]).replay_workflow(history)


@pytest.mark.asyncio
async def test_prune_failure_does_not_suppress_the_later_sweeps():
    """Each step is independent: a prune blowing up must not silently stop the
    sweeps after it."""
    result = await _run(
        CleanupConfig(retentions={"audit_log": 90}),
        _stub_dispatches,
        _stub_prune_boom,
        _stub_orphans,
        _stub_close_problems,
    )

    assert result["prune_status"] == "failed"
    assert result["interactions_archived"] == 1
    assert result["problems_closed"] == {"closed": 3, "problem_ids": ["a", "b", "c"]}


async def test_the_problem_close_sweep_runs_last_and_lands_under_its_own_key():
    """Closing a resolved problem frees its correlation key, so the sweep has
    to run even on a night the retention prune failed — the same independence
    every other sweep in this flow has."""
    result = await _run(CleanupConfig(retentions={"audit_log": 90}, problem_close_days=14))
    assert _calls["close_problems"] == [14]
    assert result["problems_closed"] == {"closed": 3, "problem_ids": ["a", "b", "c"]}


async def test_the_close_sweep_is_off_at_zero():
    result = await _run(CleanupConfig(retentions={"audit_log": 90}, problem_close_days=0))
    assert "close_problems" not in _calls
    assert "problems_closed" not in result


def _schedule_config(config: dict) -> CleanupConfig:
    """The config the schedule hands the flow, built from an `activities` row
    exactly the way `schedule_sync` builds it."""
    from aegis_worker.registry import FLOWS

    spec = next(s for s in FLOWS if s.flow is CleanupFlow)
    return spec.schedule_config({"agent_id": "pandoras-actor", "config": config, "_settings": {}})


def test_every_window_is_read_from_activities_config():
    """The flow has four windows and the registry used to pass only one of
    them, so `problem_close_days`, `interaction_orphan_days` and
    `dispatch_days` set on the `cleanup-daily` row did nothing at all (#478)."""
    cfg = _schedule_config(
        {
            "dispatch_days": 14,
            "interaction_orphan_days": 3,
            # A stored key of the retired session sweep is ignored (057 strips it).
            "task_session_days": 5,
            "problem_close_days": 2.5,
        }
    )
    assert cfg.dispatch_days == 14
    assert cfg.interaction_orphan_days == 3
    assert cfg.problem_close_days == 2.5
    assert not hasattr(cfg, "task_session_days")


def test_an_empty_or_blank_config_keeps_the_flows_own_defaults():
    """The registry's fallbacks are the dataclass's defaults, not a second copy
    of them — and a field cleared on the admin page is "not set" (#373)."""
    default = CleanupConfig()
    for config in ({}, {"dispatch_days": "", "interaction_orphan_days": None, "problem_close_days": ""}):
        cfg = _schedule_config(config)
        assert cfg.dispatch_days == default.dispatch_days
        assert cfg.interaction_orphan_days == default.interaction_orphan_days
        assert cfg.problem_close_days == default.problem_close_days


async def test_a_failing_close_sweep_is_reported_not_fatal():
    result = await _run(
        CleanupConfig(retentions={"audit_log": 90}),
        _stub_dispatches,
        _stub_prune,
        _stub_orphans,
        _stub_close_problems_boom,
    )
    assert result["problems_closed"] == {"status": "failed"}
    assert result["interactions_archived"] == 1, "the earlier sweeps still ran"
