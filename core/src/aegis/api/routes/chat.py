"""Chat endpoint — send messages to agents and browse history."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from aegis.api.auth import verify_auth
from aegis.api.sql_filters import build_where
from aegis.services.chat import (
    _capture_to_inbox_impl,
    classify_intent,
    send_message,
    synthesize_agent_reply,
)

router = APIRouter(prefix="/api/chat", dependencies=[Depends(verify_auth)])

# Agent id → Todoist label, used to tag the task captured from a Telegram chat
# ask so it's owned by the right agent and anchors that agent's downstream
# workflows. Agents absent here (none today) skip capture and stay taskless.
_AGENT_TODOIST_LABEL = {
    "pandoras-actor": "@pandora",
    "sebas": "@sebas",
    "raphael": "@raphael",
    "maou": "@maou",
}


async def _task_is_completed(pool: Any, task_id: str) -> bool:
    """True only when the task EXISTS in the local projection AND is completed.

    A missing row is treated as open: a just-created task hasn't synced into
    `todoist_tasks` yet, and we must not discard the fresh capture we just made.
    """
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT is_completed FROM todoist_tasks WHERE id = $1", task_id
            )
    except Exception:  # noqa: BLE001 — best-effort; on error assume open
        return False
    return bool(row and row["is_completed"])


async def _capture_chat_ask_as_task(
    pool: Any, target_agent: str, message: str, thread_id: str
) -> str | None:
    """Capture a Telegram chat ask as a Todoist task owned by `target_agent`.

    Returns the real Todoist task id to anchor the reply (and any workflow the
    agent spawns) to, or None to stay in taskless DM mode. Best-effort: any
    failure — kill-switch off, no inbox, no api key, a transient outbox temp-id,
    an unknown agent, or a raised exception — degrades to None so the DM reply
    still reaches the user.

    The capture is keyed on the DM `thread_id` (stable per user+agent), so a
    multi-turn conversation maps to ONE task that accumulates every reply +
    spawned-workflow link, rather than spawning a fresh task per message. If
    that thread's task has since been completed, the reused id would mirror onto
    a task the user no longer sees — so a fresh task is captured under a unique
    key instead, starting a new conversation thread.
    """
    label = _AGENT_TODOIST_LABEL.get(target_agent)
    if pool is None or label is None:
        return None
    from uuid import uuid4

    msg = (message or "").strip()
    title = msg[:120] or "(chat)"
    description = msg if len(msg) > 120 else None
    base_key = thread_id or uuid4().hex
    try:
        ref = await _capture_to_inbox_impl(
            pool=pool,
            source_tag="#telegram",
            external_id=f"tg-chat:{base_key}",
            title=title,
            description=description,
            extra_labels=[label],
        )
        if ref and not ref.startswith("item-") and await _task_is_completed(pool, ref):
            ref = await _capture_to_inbox_impl(
                pool=pool,
                source_tag="#telegram",
                external_id=f"tg-chat:{base_key}:{uuid4().hex[:8]}",
                title=title,
                description=description,
                extra_labels=[label],
            )
    except Exception:  # noqa: BLE001 — anchoring is best-effort, never block the reply
        return None
    # Only anchor to a real Todoist id; an outbox temp-id ("item-…") can't take
    # comments yet, so treat it as taskless.
    if ref and not ref.startswith("item-"):
        return ref
    return None


@router.post("/dispatches")
async def log_dispatch(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Record an outbound message into chat_history.

    Called by the Telegram bot's delivery server (and, when Slack is active,
    by the Slack delivery adapter) after a successful send.
    Persists as role='dispatch' so the same agent's chat-context loader
    (`send_message`) surfaces these alongside user/assistant turns — closes
    the gap where the user could refer to a briefing or interaction card
    they received but the model had no record of it.

    Body shape:
      agent_id     — target agent (or "system" for general-topic events)
      topic_id     — Telegram topic_id; used as chat_history.thread_id
      chat_id      — Telegram group chat_id (stored in metadata for cleanup)
      message_id   — Telegram message_id (stored in metadata for cleanup
                     via Bot API deleteMessage; may be None if the bot
                     doesn't expose it for this kind)
      content      — the actual text the user saw
      kind         — short tag (deliver, interaction_card, system_event,
                     document) used to filter for context shaping
      used_html    — whether the message rendered with HTML formatting
      delivery_ref — (optional) channel-neutral handle for cleanup/reactions,
                     e.g. {"adapter":"slack","channel":"C..","ts":".."}
                     When present, stored in metadata.delivery_ref in addition
                     to the existing telegram keys (Slack callers omit the
                     telegram keys; telegram callers omit delivery_ref).
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

    Optional `telegram: {chat_id, message_id}` block stores the user's
    incoming Telegram message_id on the user chat_history row's metadata.
    """
    agent_id = body.get("agent_id")
    message = body.get("message")
    if not agent_id or not message:
        raise HTTPException(status_code=400, detail="agent_id and message are required")

    delivery_ref = body.get("delivery_ref") or None
    telegram_meta = body.get("telegram") or None
    user_metadata: dict | None = None
    if delivery_ref is not None:
        user_metadata = {
            "kind": "user_message",
            "delivery_ref": delivery_ref,
        }
    elif telegram_meta:
        user_metadata = {
            "kind": "user_message",
            "chat_id": telegram_meta.get("chat_id"),
            "telegram_message_id": telegram_meta.get("message_id"),
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
        clickhouse_connector=getattr(request.app.state, "clickhouse_connector", None),
        search_connector=getattr(request.app.state, "search_connector", None),
        remote_script_connector=getattr(request.app.state, "remote_script_connector", None),
        vercel_connector=getattr(request.app.state, "vercel_connector", None),
        background_tasks=getattr(request.app.state, "background_tasks", None),
        user_metadata=user_metadata,
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
    user_metadata switches from `todoist_comment` to `telegram_dm`.
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
            temporal_client=temporal_client,
            remote_script_connector=getattr(
                request.app.state, "remote_script_connector", None
            ),
        )
    except (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return result


class AgentReplyTriggerRequest(BaseModel):
    """Body for POST /api/chat/agent-reply/trigger (bot → core → temporal).

    The Telegram bot's DM @mention handler hits this endpoint to spawn
    `AgentChatReplyFlow` for the named agent. The endpoint captures the ask as
    a `#telegram` Todoist task owned by the agent and anchors the flow to it
    (so the reply is mirrored there and any spawned workflow lands its links +
    logs on the same task); it falls back to taskless mode — Todoist-mirror
    step skipped — only when capture can't produce a usable task id.

    `reply_chat_id` is the Telegram chat to reply into (positive for DMs,
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
    """Capture the chat ask as a Todoist task, then spawn AgentChatReplyFlow
    anchored to it.

    The ask becomes a `#telegram`-tagged task owned by the target agent so
    Todoist is the hub for every workflow: the reply is mirrored to the task as
    a comment AND any workflow the agent spawns (e.g. `investigate_resource` →
    AlertInvestigationFlow) anchors to the same task, landing its Temporal links
    and kimi transcript there. If capture can't produce a real task id the flow
    falls back to taskless DM mode (reply still delivered).

    Returns 202 on accept with `{workflow_id, target_agent, task_id}`. The reply
    lands in Telegram asynchronously (Temporal handles durability + the 600s
    synthesize ceiling). If the Temporal client isn't wired in app state,
    returns 503 — the bot's caller treats this as "service down, fall back to
    sync /api/chat".
    """
    temporal = getattr(request.app.state, "temporal_client", None)
    if temporal is None:
        raise HTTPException(status_code=503, detail="temporal client not configured")
    from uuid import uuid4

    pool = getattr(request.app.state, "db_pool", None)
    task_id = await _capture_chat_ask_as_task(pool, body.target_agent, body.message, body.thread_id)

    workflow_id = f"agent-chat-reply-dm-{body.target_agent}-{uuid4().hex[:12]}"
    await temporal.start_workflow(
        "AgentChatReplyFlow",
        {
            "target_agent": body.target_agent,
            "synthetic_user_message": body.message,
            "thread_id": body.thread_id,
            "task_id": task_id,
            "reply_chat_id": body.reply_chat_id,
        },
        id=workflow_id,
        task_queue="aegis-main",
    )
    return {
        "workflow_id": workflow_id,
        "target_agent": body.target_agent,
        "task_id": task_id,
    }
