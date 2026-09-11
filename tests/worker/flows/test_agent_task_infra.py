"""AgentTaskFlow, infra verb: the plan from the problem, then the service check
or a read-only report.

`plan_infra_task` decides (tests/worker/activities/test_agent_task_infra_plan.py
covers each handler); the flow either checks and cards a live swarm service
exactly as before, or posts the plan's report and parks.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest_asyncio
from aegis.services import hub, hub_project
from aegis_worker.activities.agent_task import PARK_LABEL, AgentTaskActivities
from aegis_worker.activities.homelab import HomelabActivities
from aegis_worker.activities.infra_ops import InfraOpsActivities
from aegis_worker.activities.interactions import (
    ApplyTimeoutInput,
    InsertInteractionInput,
    InsertInteractionResult,
    ResolveInteractionInput,
)
from aegis_worker.flows.agent_task import AgentTaskFlow, AgentTaskFlowInput
from aegis_worker.flows.interaction import InteractionFlow
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


async def _run(events: list, plan: dict, task: dict = _ALERT_TASK) -> dict:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"tq-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[AgentTaskFlow, InteractionFlow],
            activities=_base_activities(events, plan=plan),
        ):
            return await env.client.execute_workflow(
                AgentTaskFlow.run,
                AgentTaskFlowInput(agent_id="pandoras-actor", todoist_task_id="ti-1", task=task),
                id=f"agent-task-ti-1-{uuid.uuid4()}",
                task_queue=queue,
            )


async def test_healthy_service_completes_task_without_a_card():
    """Expected to close a large share of the 30 four-week-old PROLONGED tasks."""
    events: list = []
    plan = {**_SERVICE_PLAN, "health": {"found": True, "healthy": True, "detail": "1/1"}}
    result = await _run(events, plan)

    assert result["verb"] == "infra"
    assert result["status"] == "resolved" and result["service"] == "redis_redis"
    assert any(kind == "complete" for kind, _ in events)
    assert not any(kind in ("restart", "card") for kind, _ in events)
    # The plan already carries the health check; the flow does not ask twice.
    assert not any(kind == "health" for kind, _ in events)


async def test_unhealthy_service_investigates_and_parks_pending_approval():
    """A restart-approval card is spawned; the task parks meanwhile so the
    next tick doesn't re-select it while the card is still open."""
    events: list = []
    plan = {**_SERVICE_PLAN, "health": {"found": True, "healthy": False, "detail": "0/1"}}
    result = await _run(events, plan)

    assert result["status"] == "carded"
    assert any(kind == "comment" and "boot loop" in body for kind, body in events)
    assert any(kind == "park" for kind, _ in events)


async def test_a_report_is_posted_and_the_task_parked_without_touching_docker():
    """Anything that is not a live swarm service: the plan's read-only report
    is the comment, and the task parks once with the plan's reason."""
    events: list = []
    plan = {
        "action": "report",
        "handler": "node",
        "kind": "node",
        "comment": "Node `n1`: the heartbeat has it Down. What to do: check its power.",
        "reason": "node n1 is down; a person has to look at the machine",
    }
    result = await _run(events, plan, task={**_ALERT_TASK, "content": "Swarm node n1 down"})

    assert result == {
        "task_id": "ti-1", "verb": "infra", "status": "parked", "kind": "node", "handler": "node",
    }
    assert ("comment", plan["comment"]) in events
    assert ("park", plan["reason"]) in events
    assert not any(kind in ("health", "logs", "restart", "card") for kind, _ in events)


# --- the whole path on the real activities and the real database -----------------------


class _Todoist:
    """`TodoistConnector.commands()`: takes Sync API commands, answers with
    the envelope. Records every command so the test can read the comment."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def commands(self, cmds: list[dict]) -> dict:
        self.sent.extend(cmds)
        return {
            "ok": True,
            "data": {"sync_status": {c["uuid"]: "ok" for c in cmds}},
            "error": None,
            "retryable": False,
            "external_ref": None,
        }


class _Homelab:
    """`HomelabConnector`, recording calls — a down node must get none."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def list_services(self) -> dict:
        self.calls.append("list_services")
        return {"ok": True, "data": [], "error": None, "retryable": False, "external_ref": None}

    async def service_ps(self, service_name: str) -> dict:
        self.calls.append("service_ps")
        return {"ok": True, "data": [], "error": None, "retryable": False, "external_ref": None}


@pytest_asyncio.fixture(loop_scope="function")
async def node_problem(db_pool):
    key = HomelabActivities._HEARTBEAT_STATE_KEY
    saved = await db_pool.fetchval("SELECT value FROM settings WHERE key = $1", key)
    node = f"node-{uuid.uuid4().hex[:6]}"
    task_id = f"tn-{uuid.uuid4().hex[:8]}"
    await db_pool.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = now()",
        key,
        {"nodes": {node: "Down"}, "stuck": [], "confirmed": [], "fail_count": 0},
    )
    await db_pool.execute(
        "INSERT INTO todoist_tasks (id, content, labels, source_tag, assignee_label, is_completed) "
        "VALUES ($1, $2, ARRAY['#alert','@pandora'], '#alert', '@pandora', false)",
        task_id,
        f"Swarm node {node} down",
    )
    result = await hub.ingest_event(
        db_pool,
        hub.Event(
            source="heartbeat",
            external_id=f"test-{uuid.uuid4().hex}",
            kind="occurrence",
            title=f"Swarm node {node} down",
            subject=node,
            subject_kind="node",
            klass="NodeDown",
            occurred_at=datetime.now(UTC),
        ),
    )
    await hub_project.link_task(db_pool, result.problem_id, task_id)
    yield {"node": node, "task_id": task_id}
    await db_pool.execute("DELETE FROM problem_events WHERE problem_id = $1::uuid", result.problem_id)
    await db_pool.execute("DELETE FROM problem_links WHERE problem_id = $1::uuid", result.problem_id)
    await db_pool.execute("DELETE FROM problems WHERE id = $1::uuid", result.problem_id)
    await db_pool.execute("DELETE FROM todoist_tasks WHERE id = $1", task_id)
    await db_pool.execute("DELETE FROM todoist_outbox WHERE temp_id = $1", f"agent-task-park-{task_id}")
    if saved is None:
        await db_pool.execute("DELETE FROM settings WHERE key = $1", key)
    else:
        await db_pool.execute("UPDATE settings SET value = $2 WHERE key = $1", key, saved)


async def test_a_down_node_task_runs_end_to_end_on_the_real_activities(db_pool, node_problem):
    """The prod case that parked with "there is no service to check or
    restart": a `nodedown` task. The real activities read the problem and the
    heartbeat from the database, comment what they found and park the task —
    and no Docker command reaches the node."""
    todoist, homelab = _Todoist(), _Homelab()
    act = AgentTaskActivities(
        db_pool=db_pool,
        todoist_connector=todoist,
        infra_ops=InfraOpsActivities(homelab_connector=homelab),
        homelab_connector=homelab,
    )
    task_id, node = node_problem["task_id"], node_problem["node"]
    task = dict(await act.load_task(task_id))
    task.pop("notes", None)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"tq-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[AgentTaskFlow],
            activities=[act.load_task_context, act.plan_infra_task, act.comment, act.park_task],
        ):
            result = await env.client.execute_workflow(
                AgentTaskFlow.run,
                AgentTaskFlowInput(agent_id="pandoras-actor", todoist_task_id=task_id, task=task),
                id=f"agent-task-{task_id}",
                task_queue=queue,
            )

    assert result["status"] == "parked" and result["handler"] == "node"
    notes = [c["args"]["content"] for c in todoist.sent if c["type"] == "note_add"]
    assert len(notes) == 1
    assert node in notes[0] and "Down" in notes[0]
    assert "no service to check or restart" not in notes[0]
    labels = await db_pool.fetchval("SELECT labels FROM todoist_tasks WHERE id = $1", task_id)
    assert PARK_LABEL in labels
    assert homelab.calls == []
