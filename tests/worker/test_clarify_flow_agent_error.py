"""ClarifyFlow does NOT await or clear the watermark for the abandoned
agent_chat_reply child.

Pin (2026-05-30 fix): AgentChatReplyFlow is spawned with
ParentClosePolicy.ABANDON, so the parent ClarifyFlow cannot await its
result — `child_handle.result()` raises "Result is not set." immediately.
The previous code awaited it anyway, which tripped the error compensator on
EVERY tick and cleared `todoist_tasks.last_clarified_at`, re-eligibling the
task into a 15-min duplicate-reply loop (a @pandora task burned a
claude-sonnet run every 15 min until mitigated). The fix: on a successful
spawn the watermark bump from log_classification stands and the parent never
calls clear_clarify_watermark; the abandoned child posts its own reply, and
its own error comment on permanent failure. Only a spawn-time raise (the
child never started) rolls the watermark back — that path is covered in
test_clarify_flow_agent_spawn.py::test_clarify_flow_compensates_on_spawn_failure.

Kept in a separate file from test_clarify_flow_agent_spawn.py because
Temporal does not allow two @workflow.defn classes with the same
`name="AgentChatReplyFlow"` in the same process — the other file already
registers an ok-returning stub.
"""

from __future__ import annotations

import uuid

import pytest
from aegis_worker.flows.clarify import ClarifyConfig, ClarifyFlow
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker


@workflow.defn(name="AgentChatReplyFlow")
class StubAgentChatReplyErrors:
    """Stub that completes successfully but returns status=error, matching
    AgentChatReplyFlow.run when synth fails permanently. The parent must NOT
    observe this (it never awaits the abandoned child)."""

    @workflow.run
    async def run(self, inp) -> dict:
        return {"status": "error", "reason": "synth_refused"}


def _make_acts(found, decision_for, outcome_for, child_log):
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
async def test_clarify_flow_does_not_clear_watermark_on_abandoned_child():
    """Even when AgentChatReplyFlow would complete with status="error", the
    parent ClarifyFlow neither awaits the abandoned child nor clears the
    watermark. Awaiting an ABANDON child raised "Result is not set." every
    tick and cleared the watermark, looping the task; the bump must stand on a
    successful spawn so the task is not re-processed."""
    task = {
        "id": "task-err",
        "content": "x",
        "labels": ["@raphael"],
        "source_tag": "#manual",
        "latest_user_note": "comment",
    }
    decision = {
        "classification": "raphael_followup",
        "confidence": 1.0,
        "assignee": "@raphael",
        "contexts": ["@deep"],
        "reason": "",
        "llm_model": "rules",
    }
    outcome = {
        "applied": True,
        "interaction_spawned": True,
        "interaction_payload": {
            "spawn_kind": "agent_chat_reply",
            "target_agent": "raphael",
            "task_id": "task-err",
            "synthetic_input": "u",
            "thread_id": "todoist-task-task-err",
        },
        "commands_sent": 0,
        "outbox_queued": 0,
    }

    child_log: list = []

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"tq-{uuid.uuid4().hex[:8]}"
        acts = _make_acts([task], lambda t: decision, lambda t: outcome, child_log)

        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[ClarifyFlow, StubAgentChatReplyErrors],
            activities=acts,
        ):
            result = await env.client.execute_workflow(
                ClarifyFlow.run,
                ClarifyConfig(agent_id="sebas"),
                id=f"clarify-err-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    # Spawn issued (counter incremented) ...
    assert result["interactions"] == 1
    # ... but the parent must NOT clear the watermark: it no longer awaits the
    # abandoned child (which would raise "Result is not set." every tick).
    cleared = [c for c in child_log if c.get("cleared") == "task-err"]
    assert cleared == [], f"parent must not clear watermark for abandoned child, got log={child_log!r}"
