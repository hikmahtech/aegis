"""Chat endpoint — send messages to agents and browse history."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from aegis.api.auth import verify_auth
from aegis.api.sql_filters import build_where
from aegis.services.chat import (
    classify_intent,
    send_message,
    synthesize_agent_reply,
)

router = APIRouter(prefix="/api/chat", dependencies=[Depends(verify_auth)])


@router.post("/dispatches")
async def log_dispatch(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Record an outbound message into chat_history.

    Called by the comms delivery server (Slack adapter) after a
    successful send.
    Persists as role='dispatch' so the same agent's chat-context loader
    (`send_message`) surfaces these alongside user/assistant turns — closes
    the gap where the user could refer to a briefing or interaction card
    they received but the model had no record of it.

    Body shape:
      agent_id     — target agent (or "system" for general-topic events)
      topic_id     — legacy numeric topic id; used as chat_history.thread_id
      chat_id      — legacy chat id (stored in metadata for cleanup)
      message_id   — legacy numeric message id (stored in metadata under
                     the legacy `telegram_message_id` key; may be None)
      content      — the actual text the user saw
      kind         — short tag (deliver, interaction_card, system_event,
                     document) used to filter for context shaping
      used_html    — whether the message rendered with HTML formatting
      delivery_ref — (optional) channel-neutral handle for cleanup/reactions,
                     e.g. {"adapter":"slack","channel":"C..","ts":".."}
                     When present, stored in metadata.delivery_ref in addition
                     to the legacy keys above.
    """
    pool = request.app.state.db_pool
    agent_id = body.get("agent_id") or "system"
    topic_id = body.get("topic_id")
    content = body.get("content")
    if content is None:
        raise HTTPException(status_code=400, detail="content is required")
    metadata: dict[str, Any] = {
        "kind": body.get("kind") or "deliver",
        "chat_id": body.get("chat_id"),
        "telegram_message_id": body.get("message_id"),
        "used_html": body.get("used_html", True),
    }
    delivery_ref = body.get("delivery_ref")
    if delivery_ref is not None:
        metadata["delivery_ref"] = delivery_ref
    thread_id = str(topic_id) if topic_id is not None else "system"
    await pool.execute(
        "INSERT INTO chat_history (agent_id, thread_id, role, content, metadata) "
        "VALUES ($1, $2, 'dispatch', $3, $4)",
        agent_id,
        thread_id,
        content,
        metadata,
    )
    return {"ok": True}


@router.post("")
async def chat(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Send a message to an agent.

    Optional `delivery_ref` block stores the channel-neutral handle of the
    user's incoming message on the user chat_history row's metadata.
    """
    agent_id = body.get("agent_id")
    message = body.get("message")
    if not agent_id or not message:
        raise HTTPException(status_code=400, detail="agent_id and message are required")

    delivery_ref = body.get("delivery_ref") or None
    user_metadata: dict | None = None
    if delivery_ref is not None:
        user_metadata = {
            "kind": "user_message",
            "delivery_ref": delivery_ref,
        }

    llm = getattr(request.app.state, "llm", None)
    result = await send_message(
        request.app.state.db_pool,
        llm,
        agent_id,
        message,
        thread_id=body.get("thread_id"),
        knowledge_connector=getattr(request.app.state, "knowledge_connector", None),
        settings=getattr(request.app.state, "settings", None),
        temporal_client=getattr(request.app.state, "temporal_client", None),
        finance_connector=getattr(request.app.state, "finance_connector", None),
        search_connector=getattr(request.app.state, "search_connector", None),
        remote_script_connector=getattr(request.app.state, "remote_script_connector", None),
        vercel_connector=getattr(request.app.state, "vercel_connector", None),
        mcp_manager=getattr(request.app.state, "mcp_manager", None),
        background_tasks=getattr(request.app.state, "background_tasks", None),
        user_metadata=user_metadata,
        tier_override=(body.get("tier") or None),
    )
    if result.get("error"):
        raise HTTPException(status_code=500, detail=result["error"])
    return result


@router.post("/route")
async def route_intent(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Classify a message's intent → the best-fit agent_id (front-door routing)."""
    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    llm = getattr(request.app.state, "llm", None)
    settings = getattr(request.app.state, "settings", None)
    pool = getattr(request.app.state, "db_pool", None)
    return await classify_intent(message, llm, settings, pool=pool)


@router.post("/messages/{message_id}/delivery-ref")
async def attach_delivery_ref(
    request: Request, message_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    """Patch a chat_history row's metadata with a channel-neutral delivery_ref.

    Used by adapters (Slack, and in future any other channel) after sending a
    message to store the handle needed for cleanup and reactions.

    Body shape:
      delivery_ref — required dict, e.g.
                     {"adapter":"slack","channel":"C..","ts":".."}

    Returns {"ok": True}; 404 if the row is not found; 400 if delivery_ref
    is missing from the body.
    """
    pool = request.app.state.db_pool
    delivery_ref = body.get("delivery_ref")
    if delivery_ref is None:
        raise HTTPException(status_code=400, detail="delivery_ref is required")

    status = await pool.execute(
        """
        UPDATE chat_history
        SET metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object(
            'delivery_ref', $1::jsonb
        )
        WHERE id = $2::uuid
        """,
        delivery_ref,
        message_id,
    )
    updated = status.split()[-1] if status else "0"
    if updated == "0":
        raise HTTPException(status_code=404, detail="chat_history row not found")
    return {"ok": True}


@router.get("/threads")
async def list_threads(
    request: Request,
    agent_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List chat threads with message counts."""
    pool = request.app.state.db_pool
    where, params = build_where({"agent_id": agent_id})
    idx = len(params) + 1
    params.append(limit)
    rows = await pool.fetch(
        f"""SELECT agent_id, thread_id, COUNT(*) as message_count,
                   MIN(created_at) as first_message, MAX(created_at) as last_message
            FROM chat_history{where}
            GROUP BY agent_id, thread_id
            ORDER BY MAX(created_at) DESC LIMIT ${idx}""",
        *params,
    )
    return [dict(r) for r in rows]


@router.get("/history")
async def get_thread_history(
    request: Request,
    thread_id: str | None = None,
    agent_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Get messages for a specific chat thread."""
    if not thread_id:
        raise HTTPException(status_code=400, detail="thread_id is required")

    pool = request.app.state.db_pool
    conditions = ["thread_id = $1"]
    params: list[Any] = [thread_id]
    idx = 2

    if agent_id:
        conditions.append(f"agent_id = ${idx}")
        params.append(agent_id)
        idx += 1

    where = " AND ".join(conditions)
    params.append(limit)
    rows = await pool.fetch(
        f"SELECT * FROM chat_history WHERE {where} ORDER BY created_at ASC LIMIT ${idx}",
        *params,
    )
    return [dict(r) for r in rows]


class AgentReplyRequest(BaseModel):
    """Body for POST /api/chat/agent-reply (worker → core).

    Used by AgentChatReplyFlow's synthesize_reply activity. The route is
    a thin shim around services.chat.synthesize_agent_reply — it exists so
    the worker can call core via HTTP without importing core's chat module.

    `task_id` is None on the DM (taskless) path — surface tag in
    user_metadata switches from `todoist_comment` to `chat_dm`.
    """

    agent_id: str
    message: str
    thread_id: str
    task_id: str | None = None


@router.post("/agent-reply")
async def post_agent_reply(
    body: AgentReplyRequest,
    request: Request,
) -> dict[str, Any]:
    """Synthesize a chat reply from <agent_id> for a Todoist comment channel.

    Returns 200 on success OR on PERMANENT failure (agent-not-found, LLM
    refusal/empty) — the body's `error` field signals these. Raises 5xx
    on TRANSIENT failures (LLM proxy 5xx, connect, timeout) so the worker
    activity retries via STANDARD policy.
    """
    pool = request.app.state.db_pool
    llm = getattr(request.app.state, "llm", None)
    temporal_client = getattr(request.app.state, "temporal_client", None)
    try:
        result = await synthesize_agent_reply(
            pool=pool,
            llm_client=llm,
            agent_id=body.agent_id,
            message=body.message,
            thread_id=body.thread_id,
            task_id=body.task_id,
            settings=getattr(request.app.state, "settings", None),
            temporal_client=temporal_client,
            knowledge_connector=getattr(request.app.state, "knowledge_connector", None),
            finance_connector=getattr(request.app.state, "finance_connector", None),
            search_connector=getattr(request.app.state, "search_connector", None),
            remote_script_connector=getattr(
                request.app.state, "remote_script_connector", None
            ),
            vercel_connector=getattr(request.app.state, "vercel_connector", None),
            mcp_manager=getattr(request.app.state, "mcp_manager", None),
        )
    except (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return result


class AgentReplyTriggerRequest(BaseModel):
    """Body for POST /api/chat/agent-reply/trigger (bot → core → temporal).

    The chat DM @mention handler hits this endpoint to spawn
    `AgentChatReplyFlow` for the named agent, always taskless.

    `reply_chat_id` is the legacy chat id to reply into (positive for DMs,
    negative for groups). `thread_id` is the conversation grouping key used
    by `chat_history`; the bot synthesizes a stable per-(user,agent) value
    so successive DM turns share context.
    """

    target_agent: str
    message: str
    thread_id: str
    reply_chat_id: int


@router.post("/agent-reply/trigger")
async def post_agent_reply_trigger(
    body: AgentReplyTriggerRequest,
    request: Request,
) -> dict[str, Any]:
    """Spawn AgentChatReplyFlow for a chat ask. No task is created.

    A message to an agent is a conversation, not a commitment. This endpoint
    used to capture every one as a `#chat` Todoist task before the flow
    started, on the theory that Todoist should anchor the reply and anything
    the agent went on to spawn. In practice it turned questions into chores:
    a stable per-conversation key meant the first task was reused, but once
    the user completed it every later message minted a fresh task under a
    random key, so one Slack channel accumulated 29 tasks named after
    passing questions.

    The agent decides instead. It holds `capture_to_inbox` and calls it when
    the exchange produced work worth keeping; when it does not, the
    conversation leaves nothing behind. The flow runs in its documented
    taskless mode (`task_id=None`): the Todoist mirror and error-comment
    steps are skipped and the reply reaches the user over the agent's
    channel.

    The task path is not gone — ClarifyFlow still starts the same flow WITH a
    task id when the user comments on a Todoist task, and that reply is still
    mirrored there.

    Returns 202 on accept with `{workflow_id, target_agent, task_id}`, where
    `task_id` is always null. The reply lands in chat asynchronously (Temporal
    handles durability + the 600s synthesize ceiling). If the Temporal client
    isn't wired in app state, returns 503 — the bot's caller treats this as
    "service down, fall back to sync /api/chat".
    """
    temporal = getattr(request.app.state, "temporal_client", None)
    if temporal is None:
        raise HTTPException(status_code=503, detail="temporal client not configured")
    from uuid import uuid4

    workflow_id = f"agent-chat-reply-dm-{body.target_agent}-{uuid4().hex[:12]}"
    await temporal.start_workflow(
        "AgentChatReplyFlow",
        {
            "target_agent": body.target_agent,
            "synthetic_user_message": body.message,
            "thread_id": body.thread_id,
            "task_id": None,
            "reply_chat_id": body.reply_chat_id,
        },
        id=workflow_id,
        task_queue="aegis-main",
    )
    return {
        "workflow_id": workflow_id,
        "target_agent": body.target_agent,
        # Always null. Kept in the shape so the comms client, which logs it,
        # does not need a release in lockstep with this one.
        "task_id": None,
    }
