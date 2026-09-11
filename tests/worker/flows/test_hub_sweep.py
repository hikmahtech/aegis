"""HubSweepFlow: promote, read completed tasks back, project, then group —
and only group on a yes."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

# This module defines a workflow of its own, so the sandbox re-imports it.
# Anything that pulls in asyncpg MUST pass through, or the sandbox corrupts
# asyncpg's C extensions and the next DB call segfaults.
with workflow.unsafe.imports_passed_through():
    from aegis.services.hub import Event, get_problem, ingest_event
    from aegis.services.hub_project import link_task
    from aegis_worker.activities.hub import HubActivities
    from aegis_worker.flows.hub_sweep import HubSweepConfig, HubSweepFlow

_calls: list[str] = []

_CANDIDATE = {
    "class": "stuck_post",
    "subject_kind": "post",
    "group_key": "stuck_post:post",
    "member_count": 3,
    "members": [
        {"id": "p1", "subject": "a", "title": "Post a stuck", "occurrences": 1},
        {"id": "p2", "subject": "b", "title": "Post b stuck", "occurrences": 1},
        {"id": "p3", "subject": "c", "title": "Post c stuck", "occurrences": 1},
    ],
}


@activity.defn(name="promote_expired_suppressions")
async def _promote() -> dict:
    _calls.append("promote")
    return {"promoted": 2, "problem_ids": ["a", "b"]}


@activity.defn(name="reconcile_completed_tasks")
async def _reconcile() -> dict:
    _calls.append("reconcile")
    return {"resolved": 1, "problem_ids": ["c"], "tasks_reopened": 1}


@activity.defn(name="project_pending")
async def _project() -> dict:
    _calls.append("project")
    return {"projected": 3, "created": 1, "errors": 0}


_verify_args: list[tuple[float, float]] = []


@activity.defn(name="verify_fixes")
async def _verify(window_hours: float, grace_hours: float) -> dict:
    _calls.append("verify")
    _verify_args.append((window_hours, grace_hours))
    return {"resolved": 1, "reopened": 1, "problem_ids": ["d", "e"]}


def _judge(agreed: bool):
    @activity.defn(name="judge_group")
    async def judge(candidate: dict) -> dict:
        _calls.append("judge")
        return {
            "group": agreed,
            "title": "3 posts stuck in Postiz",
            "reason": "the queue stopped draining",
            "group_key": candidate["group_key"],
        }

    return judge


def _finder(candidates: list[dict]):
    @activity.defn(name="find_group_candidates")
    async def find(min_members: int, hours: float) -> list[dict]:
        _calls.append("find")
        return candidates

    return find


@activity.defn(name="apply_group")
async def _apply(candidate: dict, verdict: dict) -> dict:
    _calls.append("apply")
    return {
        "grouped": True,
        "problem_id": "p1",
        "group_key": candidate["group_key"],
        "title": verdict["title"],
        "folded": 2,
        "tasks_retired": 2,
    }


async def _run(
    activities: list,
    workflows: tuple = (HubSweepFlow,),
    flow=HubSweepFlow,
    config: HubSweepConfig | None = None,
):
    """Run one sweep; returns `(result, history)`."""
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=f"hub-{uuid.uuid4()}",
            workflows=list(workflows),
            activities=activities,
        ) as worker,
    ):
        handle = await env.client.start_workflow(
            flow.run,
            config or HubSweepConfig(agent_id="pandoras-actor"),
            id=f"hub-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        result = await handle.result()
        return result, await handle.fetch_history()


@pytest.mark.asyncio
async def test_sweep_promotes_then_projects_and_reports():
    _calls.clear()
    _verify_args.clear()
    out, _ = await _run(
        [_promote, _reconcile, _verify, _project, _finder([]), _judge(True), _apply]
    )
    assert out == {
        "promoted": 2,
        "task_completed": 1,
        "task_reopened": 1,
        "fix_resolved": 1,
        "fix_reopened": 1,
        "projected": 3,
        "created": 1,
        "errors": 0,
        "group_candidates": 0,
        "grouped": 0,
        "folded": 0,
    }
    # Promotion first, so a just-promoted problem gets its task in the same
    # tick; completed tasks and merged fixes next, so what they resolve or
    # reopen is projected in the same tick too; grouping last, on problems
    # that already have their tasks.
    assert _calls == ["promote", "reconcile", "verify", "project", "find"]
    # The generic defaults when the hub sweep row sets nothing.
    assert _verify_args == [(24.0, 1.0)]


@pytest.mark.asyncio
async def test_sweep_verifies_fixes_with_the_rows_windows():
    _calls.clear()
    _verify_args.clear()
    await _run(
        [_promote, _reconcile, _verify, _project, _finder([]), _judge(True), _apply],
        config=HubSweepConfig(agent_id="pandoras-actor", fix_verify_hours=48.0, fix_grace_hours=6.0),
    )
    assert _verify_args == [(48.0, 6.0)]


def test_the_fix_windows_are_read_from_activities_config():
    from aegis_worker.registry import FLOWS

    spec = next(s for s in FLOWS if s.flow is HubSweepFlow)
    row = {"agent_id": "pandoras-actor", "_settings": {}}
    cfg = spec.schedule_config({**row, "config": {"fix_verify_hours": 12, "fix_grace_hours": "0.5"}})
    assert (cfg.fix_verify_hours, cfg.fix_grace_hours) == (12.0, 0.5)
    # A blank field on the admin page is "not set": the flow's own defaults.
    default = HubSweepConfig()
    for config in ({}, {"fix_verify_hours": "", "fix_grace_hours": None}):
        cfg = spec.schedule_config({**row, "config": config})
        assert (cfg.fix_verify_hours, cfg.fix_grace_hours) == (
            default.fix_verify_hours,
            default.fix_grace_hours,
        )


@pytest.mark.asyncio
async def test_sweep_groups_a_cluster_the_judge_agrees_on():
    _calls.clear()
    out, _ = await _run(
        [_promote, _reconcile, _verify, _project, _finder([_CANDIDATE]), _judge(True), _apply]
    )
    assert out["grouped"] == 1
    assert out["folded"] == 2
    assert out["group_candidates"] == 1
    assert _calls == ["promote", "reconcile", "verify", "project", "find", "judge", "apply"]


@pytest.mark.asyncio
async def test_sweep_leaves_a_cluster_the_judge_rejects_alone():
    """Three alerts that look alike are not automatically one problem. A `no`
    from the judge must not merge anything."""
    _calls.clear()
    out, _ = await _run(
        [_promote, _reconcile, _verify, _project, _finder([_CANDIDATE]), _judge(False), _apply]
    )
    assert out["group_candidates"] == 1
    assert out["grouped"] == 0
    assert "apply" not in _calls


# --- the completed-task step (#473) --------------------------------------------


@workflow.defn(name="HubSweepFlow")
class _SweepBeforeCompletedTasks:
    """HubSweepFlow as it ran before step 2 existed: the same activities in
    the same order, minus `reconcile_completed_tasks`. Kept so a history it
    wrote can be replayed against today's flow."""

    @workflow.run
    async def run(self, config: HubSweepConfig) -> dict:
        short = timedelta(seconds=30)
        await workflow.execute_activity(
            "promote_expired_suppressions", start_to_close_timeout=short
        )
        await workflow.execute_activity("project_pending", start_to_close_timeout=short)
        await workflow.execute_activity(
            "find_group_candidates", args=[0, 0.0], start_to_close_timeout=short
        )
        return {}


@pytest.mark.asyncio
async def test_a_sweep_started_before_the_deploy_replays_on_the_new_worker():
    """HubSweepFlow runs every five minutes, so a deploy can land mid-run and
    the new worker replays a history with no `reconcile_completed_tasks` in
    it. The step is behind `workflow.patched`, which is what keeps that replay
    deterministic.

    Falsifiable: call the activity without the `patched` guard and this
    replay raises a nondeterminism error.
    """
    _, history = await _run(
        [_promote, _project, _finder([])],
        workflows=(_SweepBeforeCompletedTasks,),
        flow=_SweepBeforeCompletedTasks,
    )
    await Replayer(workflows=[HubSweepFlow]).replay_workflow(history)


@workflow.defn(name="HubSweepFlow")
class _SweepBeforeFixVerification:
    """HubSweepFlow as it ran before #502: the completed-task step behind its
    patch, and no `verify_fixes`. Kept so a history it wrote can be replayed
    against today's flow."""

    @workflow.run
    async def run(self, config: HubSweepConfig) -> dict:
        short = timedelta(seconds=30)
        await workflow.execute_activity(
            "promote_expired_suppressions", start_to_close_timeout=short
        )
        if workflow.patched("hub-sweep-completed-tasks"):
            await workflow.execute_activity(
                "reconcile_completed_tasks", start_to_close_timeout=short
            )
        await workflow.execute_activity("project_pending", start_to_close_timeout=short)
        await workflow.execute_activity(
            "find_group_candidates", args=[0, 0.0], start_to_close_timeout=short
        )
        return {}


@pytest.mark.asyncio
async def test_a_sweep_started_before_fix_verification_replays_on_the_new_worker():
    """#502 put `verify_fixes` between the completed-task step and projection.
    A sweep in flight across the deploy replays a history without it, so the
    step is behind `workflow.patched`.

    Falsifiable: call the activity without the guard and this replay raises a
    nondeterminism error.
    """
    _, history = await _run(
        [_promote, _reconcile, _project, _finder([])],
        workflows=(_SweepBeforeFixVerification,),
        flow=_SweepBeforeFixVerification,
    )
    await Replayer(workflows=[HubSweepFlow]).replay_workflow(history)


@pytest.mark.asyncio
async def test_the_new_sweep_replays_its_own_history():
    _, history = await _run(
        [_promote, _reconcile, _verify, _project, _finder([]), _judge(True), _apply]
    )
    await Replayer(workflows=[HubSweepFlow]).replay_workflow(history)


@pytest.mark.asyncio
async def test_the_sweep_resolves_a_problem_whose_task_a_person_completed(db_pool):
    """The whole step on its real path: the flow calls the real activity,
    which resolves a real problem in the test database."""
    _calls.clear()
    subject = f"svc_{uuid.uuid4().hex[:8]}"
    now = datetime.now(UTC)
    r = await ingest_event(
        db_pool,
        Event(
            source="heartbeat",
            external_id=f"{subject}@1",
            kind="occurrence",
            title=f"Service {subject} down",
            klass="DockerServiceDown",
            subject=subject,
            occurred_at=now,
        ),
        now=now,
    )
    task = f"zzs-{uuid.uuid4().hex[:8]}"
    await db_pool.execute(
        "INSERT INTO todoist_tasks (id, content, labels, is_completed, updated_at) "
        "VALUES ($1, 'down', ARRAY['#alert','@pandora'], true, now())",
        task,
    )
    assert await link_task(db_pool, r.problem_id, task)

    act = HubActivities(db_pool=db_pool)
    out, _ = await _run(
        [_promote, act.reconcile_completed_tasks, _verify, _project, _finder([]), _judge(True), _apply]
    )

    assert out["task_completed"] >= 1
    assert (await get_problem(db_pool, r.problem_id))["status"] == "resolved"
