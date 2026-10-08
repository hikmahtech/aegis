"""AgentTaskFlow's retired infra verb.

The infra lane moved to the DevOps vertical (a2-devops): `agent_task_verbs` no
longer offers `infra`, and a new run that somehow still reads it parks the task
as unrouted (`PATCH_DROP_INFRA_VERB`). A run recorded before that change took
the infra branch, and its history must still replay; that is what the branch
and its two methods are kept for.
"""

from __future__ import annotations

import uuid

from aegis_worker.activities.agent_task import AgentTaskActivities
from aegis_worker.activities.interactions import (
    ApplyTimeoutInput,
    InsertInteractionInput,
    InsertInteractionResult,
    ResolveInteractionInput,
)
from aegis_worker.flows.agent_task import (
    _PATCH_344,
    AgentTaskFlow,
    AgentTaskFlowInput,
)
from aegis_worker.flows.interaction import InteractionFlow
from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

_ALERT_TASK = {
    "id": "ti-1",
    "content": "PROLONGED: redis_redis degraded for over 2 hours",
    "description": "",
    "labels": ["@pandora"],
    "source_tag": "#alert",
    "project_id": "p1",
    "assignee_label": "@pandora",
}

_SERVICE_PLAN = {"action": "service", "service": "redis_redis"}


def _base_activities(events: list, *, plan: dict):
    @activity.defn(name="load_task_context")
    async def load_task_context(task_id: str) -> dict:
        return {"external_id": "alert-abc", "gmail_message_id": "", "subject": "",
                "subject_kind": "", "verb": "infra"}

    @activity.defn(name="plan_infra_task")
    async def plan_infra_task(task_id: str, title: str) -> dict:
        events.append(("plan", title))
        return plan

    @activity.defn(name="comment")
    async def comment(task_id: str, agent_id: str, body: str) -> dict:
        events.append(("comment", body))
        return {"ok": True}

    @activity.defn(name="park_task")
    async def park_task(task_id: str, reason: str) -> dict:
        events.append(("park", reason))
        return {"parked": True}

    @activity.defn(name="complete_task")
    async def complete_task(task_id: str) -> dict:
        events.append(("complete", task_id))
        return {"completed": True}

    @activity.defn(name="service_health")
    async def service_health(service_name: str) -> dict:
        events.append(("health", service_name))
        return {"found": True, "healthy": False, "detail": "0/1", "service": service_name}

    @activity.defn(name="service_logs")
    async def service_logs(service_name: str, lines: int = 50) -> dict:
        events.append(("logs", service_name))
        return {"logs": "boot loop"}

    @activity.defn(name="restart_service")
    async def restart_service(service_name: str) -> dict:
        events.append(("restart", service_name))
        return {"ok": True, "detail": "restarted"}

    # InteractionFlow's own activities — needed because the restart card is
    # spawned as an ABANDONED child in the same worker/task-queue; without
    # these the child has no registered activities to call.
    @activity.defn(name="insert_interaction")
    async def insert_interaction(inp: InsertInteractionInput) -> InsertInteractionResult:
        return InsertInteractionResult(interaction_id="ia-restart-1")

    @activity.defn(name="send_interaction_card")
    async def send_interaction_card(
        interaction_id: str, agent_id: str, kind: str, prompt: str, options, allow_hint=False
    ) -> dict:
        events.append(("card", prompt))
        return {"ok": True}

    @activity.defn(name="resolve_interaction")
    async def resolve_interaction(inp: ResolveInteractionInput) -> None:
        return None

    @activity.defn(name="apply_interaction_timeout")
    async def apply_interaction_timeout(inp: ApplyTimeoutInput) -> None:
        return None

    return [
        load_task_context,
        plan_infra_task,
        comment,
        park_task,
        complete_task,
        service_health,
        service_logs,
        restart_service,
        insert_interaction,
        send_interaction_card,
        resolve_interaction,
        apply_interaction_timeout,
    ]



@workflow.defn(name="AgentTaskFlow", sandboxed=False)
class _AgentTaskBeforeTheInfraVerbWent:
    """The old run's commands for an `infra` task: the context, the #344
    deprecation marker, then the infra branch. The branch is the flow's own,
    unchanged since before PATCH_DROP_INFRA_VERB."""

    _run_infra_by_kind = AgentTaskFlow._run_infra_by_kind
    _run_service = AgentTaskFlow._run_service

    @workflow.run
    async def run(self, input: AgentTaskFlowInput) -> dict:
        from datetime import timedelta

        await workflow.execute_activity(
            "load_task_context",
            args=[input.todoist_task_id],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        workflow.deprecate_patch(_PATCH_344)
        return await self._run_infra_by_kind(input, input.todoist_task_id)


async def _run(events: list, plan: dict, flow=AgentTaskFlow, task: dict = _ALERT_TASK):
    """One run; returns `(result, history)`."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"tq-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[flow, InteractionFlow],
            activities=_base_activities(events, plan=plan),
        ):
            handle = await env.client.start_workflow(
                "AgentTaskFlow",
                AgentTaskFlowInput(agent_id="sebas", todoist_task_id="ti-1", task=task),
                id=f"agent-task-ti-1-{uuid.uuid4()}",
                task_queue=queue,
            )
            return await handle.result(), await handle.fetch_history()


async def test_a_new_run_parks_an_infra_task_as_unrouted():
    """Never the plan, the swarm or a restart card: the task is the user's."""
    events: list = []
    result, history = await _run(events, {"action": "report"})
    assert result == {"task_id": "ti-1", "verb": "infra", "status": "parked"}
    assert not any(kind in ("plan", "health", "logs", "restart", "card") for kind, _ in events)
    assert any(kind == "comment" and "this one is yours" in body for kind, body in events)
    assert any(kind == "park" for kind, _ in events)
    await Replayer(workflows=[AgentTaskFlow, InteractionFlow]).replay_workflow(history)


async def test_a_run_that_posted_an_infra_report_replays():
    events: list = []
    plan = {
        "action": "report", "handler": "node", "kind": "node",
        "comment": "Node `n1` is down.", "reason": "node n1 is down",
    }
    result, history = await _run(events, plan, flow=_AgentTaskBeforeTheInfraVerbWent)
    assert result["status"] == "parked" and ("plan", _ALERT_TASK["content"]) in events
    await Replayer(workflows=[AgentTaskFlow, InteractionFlow]).replay_workflow(history)


async def test_a_run_that_carded_a_restart_replays():
    """The old service path: logs, a comment, a restart card as an abandoned
    child, then the park. Falsifiable: drop the legacy branch in `run` and
    this fails with a nondeterminism error."""
    events: list = []
    plan = {**_SERVICE_PLAN, "health": {"found": True, "healthy": False, "detail": "0/1"}}
    result, history = await _run(events, plan, flow=_AgentTaskBeforeTheInfraVerbWent)
    assert result["status"] == "carded"
    await Replayer(workflows=[AgentTaskFlow, InteractionFlow]).replay_workflow(history)


async def test_the_plan_stub_parks_with_a_note():
    """`plan_infra_task` stays registered one release so an old run that still
    has to schedule it gets a plan that parks the task."""
    plan = await AgentTaskActivities().plan_infra_task("ti-1", "anything")
    assert plan["action"] == "report"
    assert "DevOps" in plan["comment"]
