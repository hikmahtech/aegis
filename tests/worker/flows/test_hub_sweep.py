"""HubSweepFlow: promote, project, then group — and only group on a yes."""

from __future__ import annotations

import uuid

import pytest
from aegis_worker.flows.hub_sweep import HubSweepConfig, HubSweepFlow
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

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


@activity.defn(name="project_pending")
async def _project() -> dict:
    _calls.append("project")
    return {"projected": 3, "created": 1, "errors": 0}


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


async def _run(activities: list) -> dict:
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=f"hub-{uuid.uuid4()}",
            workflows=[HubSweepFlow],
            activities=activities,
        ) as worker,
    ):
        return await env.client.execute_workflow(
            HubSweepFlow.run,
            HubSweepConfig(agent_id="pandoras-actor"),
            id=f"hub-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )


@pytest.mark.asyncio
async def test_sweep_promotes_then_projects_and_reports():
    _calls.clear()
    out = await _run([_promote, _project, _finder([]), _judge(True), _apply])
    assert out == {
        "promoted": 2,
        "projected": 3,
        "created": 1,
        "errors": 0,
        "group_candidates": 0,
        "grouped": 0,
        "folded": 0,
    }
    # Promotion first, so a just-promoted problem gets its task in the same
    # tick; grouping last, on problems that already have their tasks.
    assert _calls == ["promote", "project", "find"]


@pytest.mark.asyncio
async def test_sweep_groups_a_cluster_the_judge_agrees_on():
    _calls.clear()
    out = await _run([_promote, _project, _finder([_CANDIDATE]), _judge(True), _apply])
    assert out["grouped"] == 1
    assert out["folded"] == 2
    assert out["group_candidates"] == 1
    assert _calls == ["promote", "project", "find", "judge", "apply"]


@pytest.mark.asyncio
async def test_sweep_leaves_a_cluster_the_judge_rejects_alone():
    """Three alerts that look alike are not automatically one problem. A `no`
    from the judge must not merge anything."""
    _calls.clear()
    out = await _run([_promote, _project, _finder([_CANDIDATE]), _judge(False), _apply])
    assert out["group_candidates"] == 1
    assert out["grouped"] == 0
    assert "apply" not in _calls
