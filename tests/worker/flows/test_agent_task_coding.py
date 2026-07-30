"""Coding verb end to end: plan gate, implement, PR gate."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

# Module is `interactions` (PLURAL), and these are imported inside
# imports_passed_through — mirror tests/worker/flows/test_alert_investigation_gates.py:22.
with workflow.unsafe.imports_passed_through():
    from aegis_worker.activities.interactions import (
        ApplyTimeoutInput,
        InsertInteractionInput,
        InsertInteractionResult,
        ResolveInteractionInput,
        ResolveInteractionResult,
    )
    from aegis_worker.flows.agent_task import AgentTaskFlow, AgentTaskFlowInput
    from aegis_worker.flows.interaction import InteractionFlow

_CODE_TASK = {
    "id": "tc-1",
    "content": "Fix phantom EPS downgrade",
    "description": "duplicate current=true rows",
    "labels": ["@pandora", "@code"],
    "source_tag": None,
    "project_id": "pr-bcp",
    "assignee_label": "@pandora",
}


def _activities(events: list, *, plan_choice: str):
    @activity.defn(name="load_task_context")
    async def load_task_context(task_id: str) -> dict:
        return {"external_id": "", "fingerprint": "", "gmail_message_id": ""}

    @activity.defn(name="comment")
    async def comment(task_id: str, agent_id: str, body: str) -> dict:
        events.append(("comment", body))
        return {"ok": True}

    @activity.defn(name="park_task")
    async def park_task(task_id: str, reason: str) -> dict:
        events.append(("park", reason))
        return {"parked": True}

    @activity.defn(name="resolve_task_repo")
    async def resolve_task_repo(task: dict) -> dict:
        return {
            "github_repo": "Stockopedia/bcp",
            "repo_path": "Stockopedia/bcp",
            "source": "project_map",
            "candidates": [],
        }

    @activity.defn(name="run_task_investigation")
    async def run_task_investigation(
        task_id: str, title: str, description: str, repo_path: str, github_repo: str
    ) -> dict:
        events.append(("investigate", repo_path))
        return {
            "status": "running",
            "transcript": "",
            "run_id": "r1",
            "output_file": "/tmp/r1.jsonl",
            "host": "node-a",
        }

    @activity.defn(name="collect_coding_run")
    async def collect_coding_run(output_file: str, host: str, max_polls: int = 40) -> dict:
        return {"status": "succeeded", "transcript": "Plan: dedupe the rows\nSTATUS: scoped"}

    @activity.defn(name="run_task_implementation")
    async def run_task_implementation(
        task_id: str,
        title: str,
        description: str,
        plan: str,
        repo_path: str,
        github_repo: str,
    ) -> dict:
        events.append(("implement", plan[:20]))
        return {
            "status": "running",
            "transcript": "",
            "branch": "aegis-task/tc-1",
            "run_id": "r2",
            "output_file": "/tmp/r2.jsonl",
            "host": "node-a",
        }

    # stage_pending_pr returns a PLAIN STRING id, not a dict.
    @activity.defn(name="stage_pending_pr")
    async def stage_pending_pr(inp) -> str:
        return "pr-uuid-stub"

    @activity.defn(name="create_github_pr")
    async def create_github_pr(inp) -> dict:
        events.append(("pr", "opened"))
        return {"pr_url": "https://github.com/Stockopedia/bcp/pull/1", "status": "opened"}

    # InteractionFlow's own activities. Names and input types copied from the
    # canonical stub block in tests/worker/flows/test_alert_investigation_gates.py
    # (~line 215) — read that file and mirror it rather than inventing names.
    @activity.defn(name="insert_interaction")
    async def insert_interaction(inp: InsertInteractionInput) -> InsertInteractionResult:
        events.append(("insert_ia", inp.origin))
        return InsertInteractionResult(interaction_id="ia-coding-test")

    @activity.defn(name="send_interaction_card")
    async def send_interaction_card(
        interaction_id: str,
        agent_id: str,
        kind: str,
        prompt: str,
        options,
        allow_hint: bool = False,
    ) -> dict:
        return {"ok": True, "message_id": 1}

    @activity.defn(name="resolve_interaction")
    async def resolve_interaction(inp: ResolveInteractionInput) -> ResolveInteractionResult:
        return ResolveInteractionResult(already_resolved=False)

    @activity.defn(name="apply_interaction_timeout")
    async def apply_interaction_timeout(inp: ApplyTimeoutInput) -> None:
        return None

    @activity.defn(name="update_interaction_delivery_ref")
    async def update_interaction_delivery_ref(*args) -> None:
        return None

    return [
        load_task_context,
        comment,
        park_task,
        resolve_task_repo,
        run_task_investigation,
        collect_coding_run,
        run_task_implementation,
        stage_pending_pr,
        create_github_pr,
        insert_interaction,
        send_interaction_card,
        resolve_interaction,
        apply_interaction_timeout,
        update_interaction_delivery_ref,
    ]


async def test_declined_plan_stops_before_any_implement_run():
    """A misread task or wrong repo must cost nothing beyond the read-only run."""
    events: list = []
    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"tq-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[AgentTaskFlow, InteractionFlow],
            activities=_activities(events, plan_choice="skip"),
        ):
            result = await env.client.execute_workflow(
                AgentTaskFlow.run,
                AgentTaskFlowInput(
                    agent_id="pandoras-actor", todoist_task_id="tc-1", task=_CODE_TASK
                ),
                id=f"agent-task-tc-1-{uuid.uuid4()}",
                task_queue=queue,
            )

    assert result["verb"] == "coding"
    assert not any(kind == "implement" for kind, _ in events)
    assert not any(kind == "pr" for kind, _ in events)
    assert any(kind == "park" for kind, _ in events)


@pytest.mark.asyncio
async def test_approved_plan_implements_then_opens_pr_and_parks():
    """The whole point of the verb: plan approved -> implement -> PR approved -> open PR."""
    events: list = []
    async with (
        await WorkflowEnvironment.start_local() as env,
        Worker(
            env.client,
            task_queue="tq-agent-task-coding",
            workflows=[AgentTaskFlow, InteractionFlow],
            activities=_activities(events, plan_choice="approve"),
        ),
    ):
        wf_id = f"agent-task-tc-1-{uuid.uuid4()}"
        handle = await env.client.start_workflow(
            AgentTaskFlow.run,
            AgentTaskFlowInput(
                agent_id="pandoras-actor", todoist_task_id="tc-1", task=_CODE_TASK
            ),
            id=wf_id,
            task_queue="tq-agent-task-coding",
        )

        plan_card = env.client.get_workflow_handle("agent-task-plan-tc-1")
        for _ in range(200):
            await asyncio.sleep(0.05)
            if any(kind == "insert_ia" and body == "agent_task_coding_plan" for kind, body in events):
                break
        await plan_card.signal(InteractionFlow.submit_response, {"value": "approve"})

        pr_card = env.client.get_workflow_handle("agent-task-pr-tc-1")
        for _ in range(200):
            await asyncio.sleep(0.05)
            if any(kind == "insert_ia" and body == "agent_task_coding_pr" for kind, body in events):
                break
        await pr_card.signal(InteractionFlow.submit_response, {"value": "approve"})

        result = await asyncio.wait_for(handle.result(), timeout=15.0)

    assert result["verb"] == "coding"
    assert result["status"] == "pr_opened"
    kinds = [kind for kind, _ in events]
    assert kinds.index("investigate") < kinds.index("implement") < kinds.index("pr")
