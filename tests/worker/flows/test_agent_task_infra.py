"""AgentTaskFlow, infra verb: healthy short-circuit and the restart gate."""

from __future__ import annotations

import uuid

from aegis_worker.flows.agent_task import AgentTaskFlow, AgentTaskFlowInput
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

_ALERT_TASK = {
    "id": "ti-1",
    "content": "PROLONGED: redis_redis degraded for over 2 hours",
    "description": "",
    "labels": ["@pandora"],
    "source_tag": "#alert",
    "project_id": "p1",
    "assignee_label": "@pandora",
}


def _base_activities(events: list, *, healthy: bool):
    @activity.defn(name="load_task_context")
    async def load_task_context(task_id: str) -> dict:
        return {"external_id": "alert-abc", "fingerprint": "abc", "gmail_message_id": ""}

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
        return {"found": True, "healthy": healthy, "detail": "1/1" if healthy else "0/1"}

    @activity.defn(name="service_logs")
    async def service_logs(service_name: str, lines: int = 50) -> dict:
        return {"logs": "boot loop"}

    @activity.defn(name="restart_service")
    async def restart_service(service_name: str) -> dict:
        events.append(("restart", service_name))
        return {"ok": True, "detail": "restarted"}

    return [
        load_task_context,
        comment,
        park_task,
        complete_task,
        service_health,
        service_logs,
        restart_service,
    ]


async def test_healthy_service_completes_task_without_a_card():
    """Expected to close a large share of the 30 four-week-old PROLONGED tasks."""
    events: list = []
    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"tq-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[AgentTaskFlow],
            activities=_base_activities(events, healthy=True),
        ):
            result = await env.client.execute_workflow(
                AgentTaskFlow.run,
                AgentTaskFlowInput(
                    agent_id="pandoras-actor", todoist_task_id="ti-1", task=_ALERT_TASK
                ),
                id=f"agent-task-ti-1-{uuid.uuid4()}",
                task_queue=queue,
            )

    assert result["verb"] == "infra"
    assert result["status"] == "resolved"
    assert any(kind == "complete" for kind, _ in events)
    assert not any(kind == "restart" for kind, _ in events)


async def test_unhealthy_service_investigates_and_parks_pending_approval():
    """No card answer in this harness ⇒ must park, never leave the task live."""
    events: list = []
    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"tq-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[AgentTaskFlow],
            activities=_base_activities(events, healthy=False),
        ):
            result = await env.client.execute_workflow(
                AgentTaskFlow.run,
                AgentTaskFlowInput(
                    agent_id="pandoras-actor", todoist_task_id="ti-1", task=_ALERT_TASK
                ),
                id=f"agent-task-ti-1-{uuid.uuid4()}",
                task_queue=queue,
            )

    assert result["status"] == "parked"
    assert any(kind == "comment" and "boot loop" in body for kind, body in events)
    assert any(kind == "park" for kind, _ in events)
