"""AgentChatReplyFlow — workflow tests covering happy + 4 failure modes."""

from __future__ import annotations

import uuid

import pytest
from aegis_worker.flows.agent_chat_reply import AgentChatReplyFlow, AgentChatReplyInput
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker


def _make_input(agent: str = "raphael") -> AgentChatReplyInput:
    return AgentChatReplyInput(
        target_agent=agent,
        task_id="abc",
        synthetic_user_message="User commented: tell me about Tigris.",
        thread_id="todoist-task-abc",
    )


def _mk_activities(
    synth_return=None,
    synth_side_effect=None,
    telegram_return=None,
    telegram_side_effect=None,
    post_return=None,
    post_side_effect=None,
    error_post_return=None,
):
    """Build mock activity callables registered with the test Worker."""

    @activity.defn(name="synthesize_reply")
    async def synthesize_reply(agent_id, message, thread_id, task_id):
        if synth_side_effect:
            raise synth_side_effect
        return synth_return or {
            "reply_text": "Reply.",
            "tool_trace_summary": "search_knowledge",
            "llm_model": "claude-sonnet",
            "error": None,
            "error_is_transient": False,
        }

    @activity.defn(name="send_telegram")
    async def send_telegram(agent_id, message, chat_id=0, keyboard=None):
        if telegram_side_effect:
            raise telegram_side_effect
        return telegram_return or {"ok": True, "message_id": 999}

    @activity.defn(name="post_agent_reply_comment")
    async def post_agent_reply_comment(
        task_id, agent_id, reply_text, tool_trace_summary, telegram_message_id
    ):
        if post_side_effect:
            raise post_side_effect
        return post_return or {"posted": True, "outbox_queued": 0}

    @activity.defn(name="post_agent_reply_error_comment")
    async def post_agent_reply_error_comment(task_id, agent_id, reason):
        return error_post_return or {"posted": True}

    return [
        synthesize_reply,
        send_telegram,
        post_agent_reply_comment,
        post_agent_reply_error_comment,
    ]


@pytest.mark.parametrize("agent", ["sebas", "raphael", "maou"])
@pytest.mark.asyncio
async def test_happy_path_per_agent(agent):
    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"tq-{uuid.uuid4().hex[:8]}"
        acts = _mk_activities()
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AgentChatReplyFlow],
            activities=acts,
        ):
            result = await env.client.execute_workflow(
                AgentChatReplyFlow.run,
                _make_input(agent),
                id=f"acf-happy-{agent}-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
        assert result["status"] == "ok"
        assert result["telegram_message_id"] == 999
        # Unified return shape: every return path must carry all four keys.
        assert set(result.keys()) == {"status", "reason", "telegram_message_id", "agent_id"}
        assert result["reason"] is None
        assert result["agent_id"] == agent


@pytest.mark.asyncio
async def test_synthesize_failure_posts_error_comment():
    """synthesize_reply raises after retries → flow runs error-comment + reports failure."""
    error_log = []

    @activity.defn(name="synthesize_reply")
    async def synth(agent_id, message, thread_id, task_id):
        raise RuntimeError("LLM proxy down")

    @activity.defn(name="send_telegram")
    async def telegram(agent_id, message, chat_id=0, keyboard=None):
        raise RuntimeError("should not be called")

    @activity.defn(name="post_agent_reply_comment")
    async def post_ok(task_id, agent_id, reply_text, tool_trace_summary, telegram_message_id):
        raise RuntimeError("should not be called")

    @activity.defn(name="post_agent_reply_error_comment")
    async def post_err(task_id, agent_id, reason):
        error_log.append({"task_id": task_id, "agent_id": agent_id, "reason": reason})
        return {"posted": True}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"tq-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AgentChatReplyFlow],
            activities=[synth, telegram, post_ok, post_err],
        ):
            result = await env.client.execute_workflow(
                AgentChatReplyFlow.run,
                _make_input(),
                id=f"acf-err-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
    assert result["status"] == "error"
    # Unified shape — synthesize-failure path carries reason + None telegram id.
    assert set(result.keys()) == {"status", "reason", "telegram_message_id", "agent_id"}
    assert result["telegram_message_id"] is None
    assert result["agent_id"] == "raphael"
    assert "LLM proxy down" in (result["reason"] or "")
    assert len(error_log) == 1
    assert error_log[0]["agent_id"] == "raphael"
    assert "LLM proxy down" in error_log[0]["reason"]


@pytest.mark.asyncio
async def test_synthesize_returns_permanent_error_posts_apology():
    """200-with-error from synthesize_reply → workflow composes apology
    via Telegram + Todoist (not via error-comment route — this is a
    soft failure)."""
    sent = {}

    @activity.defn(name="synthesize_reply")
    async def synth(agent_id, message, thread_id, task_id):
        return {
            "reply_text": "",
            "tool_trace_summary": "",
            "llm_model": "",
            "error": "Agent 'foo' not found",
            "error_is_transient": False,
        }

    @activity.defn(name="send_telegram")
    async def telegram(agent_id, message, chat_id=0, keyboard=None):
        sent["telegram"] = message
        return {"ok": True, "message_id": 1}

    @activity.defn(name="post_agent_reply_comment")
    async def post_ok(task_id, agent_id, reply_text, tool_trace_summary, telegram_message_id):
        sent["todoist"] = reply_text
        return {"posted": True, "outbox_queued": 0}

    @activity.defn(name="post_agent_reply_error_comment")
    async def post_err(task_id, agent_id, reason):
        return {"posted": True}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"tq-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AgentChatReplyFlow],
            activities=[synth, telegram, post_ok, post_err],
        ):
            result = await env.client.execute_workflow(
                AgentChatReplyFlow.run,
                _make_input(),
                id=f"acf-perm-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    # Apology text mentions the error
    assert "not found" in sent.get("telegram", "")
    assert "not found" in sent.get("todoist", "")
    # Permanent error → status="error" (per workflow contract). The
    # synth-returned error string is surfaced as `reason` and the
    # Telegram message_id is preserved (this path still posts).
    assert result["status"] == "error"
    assert set(result.keys()) == {"status", "reason", "telegram_message_id", "agent_id"}
    assert "not found" in (result["reason"] or "")
    assert result["telegram_message_id"] == 1
    assert result["agent_id"] == "raphael"


@pytest.mark.asyncio
async def test_telegram_failure_does_not_short_circuit_todoist_post():
    """Telegram failure → still post Todoist comment with telegram_message_id=None."""
    posted_with = {}

    @activity.defn(name="synthesize_reply")
    async def synth(agent_id, message, thread_id, task_id):
        return {
            "reply_text": "ok",
            "tool_trace_summary": "",
            "llm_model": "c",
            "error": None,
            "error_is_transient": False,
        }

    @activity.defn(name="send_telegram")
    async def telegram(agent_id, message, chat_id=0, keyboard=None):
        raise RuntimeError("delivery down")

    @activity.defn(name="post_agent_reply_comment")
    async def post_ok(task_id, agent_id, reply_text, tool_trace_summary, telegram_message_id):
        posted_with["telegram_message_id"] = telegram_message_id
        return {"posted": True, "outbox_queued": 0}

    @activity.defn(name="post_agent_reply_error_comment")
    async def post_err(task_id, agent_id, reason):
        return {"posted": True}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"tq-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AgentChatReplyFlow],
            activities=[synth, telegram, post_ok, post_err],
        ):
            result = await env.client.execute_workflow(
                AgentChatReplyFlow.run,
                _make_input(),
                id=f"acf-tg-fail-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert posted_with["telegram_message_id"] is None
    # status is degraded but not error — Telegram failure ≠ total failure
    assert result["status"] in {"ok", "degraded"}
    # Unified shape: degraded path carries None telegram_message_id and
    # None reason (the synth path didn't fail).
    assert set(result.keys()) == {"status", "reason", "telegram_message_id", "agent_id"}
    assert result["telegram_message_id"] is None
    assert result["reason"] is None
    assert result["agent_id"] == "raphael"


@pytest.mark.asyncio
async def test_taskless_dm_path_skips_todoist_mirror():
    """DM path: task_id=None must skip Todoist mirror + error-comment activities.
    The active comms adapter (Slack) routes the reply by the agent's channel."""
    todoist_called = {"mirror": False, "error": False}
    telegram_capture = {}

    @activity.defn(name="synthesize_reply")
    async def synth(agent_id, message, thread_id, task_id):
        # Taskless mode must propagate task_id=None to core (surface tag flip).
        assert task_id is None, "taskless path should pass task_id=None to chat service"
        return {
            "reply_text": "AEGIS is composed of three packages…",
            "tool_trace_summary": "",
            "llm_model": "claude-sonnet",
            "error": None,
            "error_is_transient": False,
        }

    @activity.defn(name="send_telegram")
    async def telegram(agent_id, message, chat_id=0, keyboard=None):
        telegram_capture["agent_id"] = agent_id
        telegram_capture["chat_id"] = chat_id
        telegram_capture["message"] = message
        return {"ok": True, "message_id": 7777}

    @activity.defn(name="post_agent_reply_comment")
    async def post_mirror(task_id, agent_id, reply_text, tool_trace_summary, telegram_message_id):
        todoist_called["mirror"] = True  # MUST NOT FIRE
        return {"posted": True}

    @activity.defn(name="post_agent_reply_error_comment")
    async def post_err(task_id, agent_id, reason):
        todoist_called["error"] = True  # MUST NOT FIRE
        return {"posted": True}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"tq-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AgentChatReplyFlow],
            activities=[synth, telegram, post_mirror, post_err],
        ):
            result = await env.client.execute_workflow(
                AgentChatReplyFlow.run,
                AgentChatReplyInput(
                    target_agent="pandoras-actor",
                    synthetic_user_message="@pandora why is gmail-ingest dropping emails?",
                    thread_id="telegram-12345-pandoras-actor",
                    task_id=None,
                ),
                id=f"acf-dm-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result["status"] == "ok"
    assert result["telegram_message_id"] == 7777
    assert result["agent_id"] == "pandoras-actor"
    # Todoist mirror MUST NOT fire on the DM path.
    assert todoist_called["mirror"] is False
    assert todoist_called["error"] is False
    # No chat override is threaded — the adapter routes by the agent's channel
    # (chat_id defaults to 0 = "use the agent's default").
    assert telegram_capture["chat_id"] == 0
    assert telegram_capture["agent_id"] == "pandoras-actor"


@pytest.mark.asyncio
async def test_taskless_dm_synthesize_failure_skips_error_comment():
    """DM path + synth failure: must NOT post error comment (no task to post to)."""
    error_post_called = {"hit": False}

    @activity.defn(name="synthesize_reply")
    async def synth(agent_id, message, thread_id, task_id):
        raise RuntimeError("upstream blew up")

    @activity.defn(name="send_telegram")
    async def telegram(agent_id, message, chat_id=0, keyboard=None):
        return {"ok": True, "message_id": 1}

    @activity.defn(name="post_agent_reply_comment")
    async def post_mirror(task_id, agent_id, reply_text, tool_trace_summary, telegram_message_id):
        return {"posted": True}

    @activity.defn(name="post_agent_reply_error_comment")
    async def post_err(task_id, agent_id, reason):
        error_post_called["hit"] = True
        return {"posted": True}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"tq-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AgentChatReplyFlow],
            activities=[synth, telegram, post_mirror, post_err],
        ):
            result = await env.client.execute_workflow(
                AgentChatReplyFlow.run,
                AgentChatReplyInput(
                    target_agent="pandoras-actor",
                    synthetic_user_message="hi",
                    thread_id="dm-thread",
                    task_id=None,
                ),
                id=f"acf-dm-err-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result["status"] == "error"
    assert "upstream blew up" in (result["reason"] or "")
    assert error_post_called["hit"] is False, "DM path must NOT fire error-comment activity"
