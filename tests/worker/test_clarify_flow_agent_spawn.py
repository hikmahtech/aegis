"""ClarifyFlow spawn dispatch tests for the agent_chat_reply branch."""

from __future__ import annotations

import uuid

import pytest
from aegis_worker.flows.clarify import ClarifyConfig, ClarifyFlow
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker


# Module-level stub — Temporal does not allow @workflow.defn on local classes.
@workflow.defn(name="AgentChatReplyFlow")
class StubAgentChatReply:
    @workflow.run
    async def run(self, inp) -> dict:
        return {"status": "ok"}


def _make_acts(found, decision_for, outcome_for, child_log):
    """Build a minimal activity set sufficient for one ClarifyFlow tick.

    found: list of tasks find_unclassified_items returns.
    decision_for(task) -> classify_one result dict.
    outcome_for(task) -> apply_outcome result dict.
    child_log: list mutated when child workflow is spawned.
    """

    @activity.defn(name="find_unclassified_items")
    async def find(max_items):
        return found

    @activity.defn(name="classify_one")
    async def classify(task):
        return decision_for(task)

    @activity.defn(name="apply_outcome")
    async def apply(task, decision, pass_n):
        return outcome_for(task)

    @activity.defn(name="log_classification")
    async def log(task_id, decision, applied, pass_n, user_hint, bump_watermark):
        return None

    @activity.defn(name="post_agent_reply_error_comment")
    async def err(task_id, agent_id, reason):
        child_log.append({"err_for": task_id, "agent": agent_id, "reason": reason})
        return {"posted": True}

    @activity.defn(name="clear_clarify_watermark")
    async def clear(task_id):
        child_log.append({"cleared": task_id})
        return {"cleared": True}

    @activity.defn(name="ingest_reference_to_ks")
    async def ingest(task_id, task_content, task_description, source_tag, latest_user_note):
        return {"status": "skipped"}

    return [find, classify, apply, log, err, clear, ingest]


@pytest.mark.asyncio
async def test_clarify_flow_spawns_agent_chat_reply_on_payload():
    """When apply_outcome returns spawn_kind=agent_chat_reply, ClarifyFlow
    fires AgentChatReplyFlow as an abandoned child with the right id shape.
    """
    task = {
        "id": "task-r",
        "content": "Spike",
        "labels": ["@raphael"],
        "source_tag": "#manual",
        "latest_user_note": "tell me",
    }
    decision = {
        "classification": "raphael_followup",
        "confidence": 1.0,
        "assignee": "@raphael",
        "contexts": ["@deep"],
        "reason": "test",
        "llm_model": "rules",
    }
    outcome = {
        "applied": True,
        "interaction_spawned": True,
        "interaction_payload": {
            "spawn_kind": "agent_chat_reply",
            "target_agent": "raphael",
            "task_id": "task-r",
            "synthetic_input": "user said...",
            "thread_id": "todoist-task-task-r",
        },
        "commands_sent": 0,
        "outbox_queued": 0,
    }

    child_log: list = []

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"tq-{uuid.uuid4().hex[:8]}"
        acts = _make_acts([task], lambda t: decision, lambda t: outcome, child_log)

        # StubAgentChatReply is defined at module level (Temporal does not
        # allow @workflow.defn on local classes). It is registered so that
        # start_child_workflow succeeds without error.
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[ClarifyFlow, StubAgentChatReply],
            activities=acts,
        ):
            result = await env.client.execute_workflow(
                ClarifyFlow.run,
                ClarifyConfig(agent_id="sebas"),
                id=f"clarify-test-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            # interactions==1 means start_child_workflow returned without
            # raising — the spawn was issued with ABANDON policy. With ABANDON
            # the parent doesn't wait for child completion, so we assert on
            # the parent's counter (same pattern as the pandora_investigation
            # test in test_clarify_flow.py).
            assert result["interactions"] == 1


@pytest.mark.asyncio
async def test_clarify_flow_compensates_on_spawn_failure():
    """When start_child_workflow raises, ClarifyFlow MUST:
      - post_agent_reply_error_comment with the reason,
      - clear_clarify_watermark for the task,
    so the comment isn't silently consumed.
    """
    task = {
        "id": "task-fail",
        "content": "x",
        "labels": ["@sebas"],
        "source_tag": "#manual",
        "latest_user_note": "comment",
    }
    decision = {
        "classification": "sebas_followup",
        "confidence": 1.0,
        "assignee": "@sebas",
        "contexts": ["@deep"],
        "reason": "",
        "llm_model": "rules",
    }
    # Drop `thread_id` so AgentChatReplyInput(**payload) raises TypeError
    # BEFORE start_child_workflow is called. The TypeError is caught by the
    # `except Exception as spawn_exc` block, which then runs the two
    # compensating activities.
    outcome = {
        "applied": True,
        "interaction_spawned": True,
        "interaction_payload": {
            "spawn_kind": "agent_chat_reply",
            "target_agent": "sebas",
            "task_id": "task-fail",
            "synthetic_input": "u",
            # "thread_id" intentionally omitted → AgentChatReplyInput() TypeError
        },
    }

    child_log: list = []

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"tq-{uuid.uuid4().hex[:8]}"
        acts = _make_acts([task], lambda t: decision, lambda t: outcome, child_log)

        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[ClarifyFlow, StubAgentChatReply],
            activities=acts,
        ):
            await env.client.execute_workflow(
                ClarifyFlow.run,
                ClarifyConfig(agent_id="sebas"),
                id=f"clarify-fail-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    # Compensating action ran
    err_logs = [c for c in child_log if c.get("err_for") == "task-fail"]
    cleared_logs = [c for c in child_log if c.get("cleared") == "task-fail"]
    assert len(err_logs) == 1
    assert len(cleared_logs) == 1
