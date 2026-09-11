"""AgentTaskFlow, `ask` verb (#344): a `#chat`/`#research`/`#calendar`/`#manual`
task given to an agent goes to that agent's own chat path.

The executor is the one clarify already uses when you comment on an agent's
task — `AgentChatReplyFlow` — so this runs the REAL flow, with only its three
outward calls (core's chat loop, the channel, the Todoist mirror) stubbed.
The task side runs on the real `AgentTaskActivities` and the real database.
"""

from __future__ import annotations

import uuid

import pytest_asyncio
from aegis_worker.activities import clarify as _clarify
from aegis_worker.activities.agent_task import PARK_LABEL, AgentTaskActivities
from aegis_worker.flows.agent_chat_reply import AgentChatReplyFlow
from aegis_worker.flows.agent_task import AgentTaskFlow, AgentTaskFlowInput
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

_AGENT = "askflow-agent"
_LABEL = "@askflow"


def _chat_activities(calls: list):
    @activity.defn(name="synthesize_reply")
    async def synthesize_reply(agent_id, message, thread_id, task_id):
        calls.append(("synthesize", agent_id, message, thread_id, task_id))
        return {"reply_text": "The cache is cold after every deploy.",
                "tool_trace_summary": "", "error": None}

    @activity.defn(name="send_message")
    async def send_message(agent_id, message, chat_id=0, thread_ref=None, thread_overflow=False):
        calls.append(("send", agent_id, message))
        return {"ok": True, "message_id": 7}

    @activity.defn(name="post_agent_reply_comment")
    async def post_agent_reply_comment(task_id, agent_id, reply_text, tool_trace_summary, message_id):
        calls.append(("mirror", task_id, reply_text))
        return {"posted": True, "outbox_queued": 0}

    @activity.defn(name="post_agent_reply_error_comment")
    async def post_agent_reply_error_comment(task_id, agent_id, reason):
        calls.append(("error", task_id, reason))
        return {"posted": True}

    return [synthesize_reply, send_message, post_agent_reply_comment, post_agent_reply_error_comment]


@pytest_asyncio.fixture(loop_scope="function")
async def chat_task(db_pool):
    task_id = f"ta-{uuid.uuid4().hex[:8]}"
    await db_pool.execute("DELETE FROM agents WHERE id = $1", _AGENT)
    await db_pool.execute(
        "INSERT INTO agents (id, name, role, system_prompt_path, active, metadata) "
        "VALUES ($1, 'Ask Flow', 'tester', '', true, $2)",
        _AGENT,
        {"mention_aliases": ["askflow"]},
    )
    await db_pool.execute(
        "INSERT INTO todoist_tasks (id, content, labels, source_tag, assignee_label, is_completed) "
        "VALUES ($1, 'Why is the cache slow after a deploy?', ARRAY['#chat', $2], '#chat', $2, false)",
        task_id,
        _LABEL,
    )
    _clarify._agent_reg_cache.update(reg=None, ts=0.0)
    yield task_id
    await db_pool.execute("DELETE FROM todoist_tasks WHERE id = $1", task_id)
    await db_pool.execute("DELETE FROM todoist_outbox WHERE temp_id = $1", f"agent-task-park-{task_id}")
    await db_pool.execute("DELETE FROM agents WHERE id = $1", _AGENT)
    _clarify._agent_reg_cache.update(reg=None, ts=0.0)


async def test_a_chat_task_is_asked_of_its_agent_and_parked(db_pool, chat_task):
    """Before #344 this task got "No executor for this task type (#chat)" and
    `@waiting`, and nobody ever answered it."""
    act = AgentTaskActivities(db_pool=db_pool)
    calls: list = []
    task = dict(await act.load_task(chat_task))
    task.pop("notes", None)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"tq-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[AgentTaskFlow, AgentChatReplyFlow],
            activities=[
                act.load_task_context, act.prepare_agent_ask, act.park_task, act.comment,
                *_chat_activities(calls),
            ],
        ):
            result = await env.client.execute_workflow(
                AgentTaskFlow.run,
                AgentTaskFlowInput(agent_id="pandoras-actor", todoist_task_id=chat_task, task=task),
                id=f"agent-task-{chat_task}",
                task_queue=queue,
            )
            # The reply is an ABANDONED child: it outlives the task flow, the
            # way a card does. Wait for it here to see what it was asked.
            reply = await env.client.get_workflow_handle(f"agent-task-ask-{chat_task}").result()

    assert result == {"task_id": chat_task, "verb": "ask", "status": "asked", "agent": _AGENT}
    assert reply["status"] == "ok"
    synth = [c for c in calls if c[0] == "synthesize"]
    assert len(synth) == 1
    _, agent, message, thread_id, task_ref = synth[0]
    assert agent == _AGENT
    assert thread_id == f"todoist-task-{chat_task}"
    assert task_ref == chat_task
    assert "Why is the cache slow after a deploy?" in message
    # The agent's answer lands on the task through the executor's own mirror.
    assert ("mirror", chat_task, "The cache is cold after every deploy.") in calls
    labels = await db_pool.fetchval("SELECT labels FROM todoist_tasks WHERE id = $1", chat_task)
    assert PARK_LABEL in labels
