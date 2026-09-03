"""Task threads — the two calls comms makes when a Slack reply lands in a thread.

A task's turns are delivered to one Slack thread, and a reply in that thread is
meant to become the next turn. Comms cannot reach the database, so it asks Core
twice: which task owns this thread root, and then post this text on it.

Two things here are load-bearing.

The note is written **verbatim** — no author prefix, no ``Workflow run:``
footer. That footer (and the clarify/agent-reply prefixes) is exactly what
``services/task_sessions.is_user_note`` uses to tell AEGIS's own comments from
the user's, so a helpfully-decorated note would land in Todoist and never start
a turn. Nothing on this path may add to the text.

And the lookup answers ``200`` with ``{"task_id": null}`` on a miss rather than
``404``: most Slack threads are not task threads, and comms falls through to its
normal routing on a miss. A 404 there would make the ordinary case look like an
error in every log and metric.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from aegis.api.auth import verify_auth
from aegis.api.deps import get_settings
from aegis.config import Settings
from aegis.connectors.todoist import TodoistConnector
from aegis.services.task_sessions import find_by_thread, get_session
from aegis.services.todoist_config import resolve_todoist_api_key

logger = structlog.get_logger()

# Todoist rejects an over-long note outright. We refuse first, with the length
# in the message so the caller can split, rather than post a truncated one: this
# route's whole contract is that what it posts is what the user typed, and a
# silently clipped reply is worse than a rejected one. Same number and same
# reasoning as the `comment_on_task` chat tool's `_COMMENT_MAX_CHARS`.
MAX_NOTE_CHARS = 15_000

router = APIRouter(
    prefix="/api/admin",
    tags=["task-sessions"],
    dependencies=[Depends(verify_auth)],
)


class CommentBody(BaseModel):
    text: str


@router.get("/task-sessions/by-thread")
async def task_by_thread(request: Request, channel: str, ts: str) -> dict[str, Any]:
    """The task whose session owns this Slack thread root, or `null` on a miss."""
    pool = request.app.state.db_pool
    return {"task_id": await find_by_thread(pool, channel, ts)}


@router.post("/tasks/{task_id}/comment")
async def comment_on_task(
    request: Request,
    task_id: str,
    body: CommentBody,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Post `text` on the task as a plain Todoist note, exactly as given.

    Exactly as given means byte for byte: the note is the caller's string, not a
    stripped copy. A Slack reply that is a fenced code block or an indented diff
    is content, and a route that tidied it would rewrite what the user typed.
    The stripped copy exists only to reject a whitespace-only reply.

    The note counts as user-authored, so the existing `note:added` webhook picks
    it up and runs the task's next turn. 404 when the task owns no session: this
    route exists to feed a session, and a silent 200 would swallow the reply.
    400 over `MAX_NOTE_CHARS`, so the caller learns the reply was too long
    instead of a clipped version landing on the task under their name.
    """
    pool = request.app.state.db_pool
    text = body.text or ""
    if not text.strip():
        raise HTTPException(status_code=400, detail="text is required")
    if len(text) > MAX_NOTE_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"text is {len(text)} chars, over Todoist's {MAX_NOTE_CHARS} limit",
        )
    if await get_session(pool, task_id) is None:
        raise HTTPException(status_code=404, detail=f"no task session for {task_id}")

    key = await resolve_todoist_api_key(pool, settings)
    if not key:
        raise HTTPException(status_code=503, detail="Todoist not configured")

    connector = TodoistConnector(api_key=key, db_pool=pool, timeout=10.0)
    cmd = TodoistConnector.build_note_add_command(task_id, text)
    try:
        result = await connector.commands([cmd])
    finally:
        await connector.close()
    status = TodoistConnector.check_sync_status(result, [cmd["uuid"]])
    if not status["ok"]:
        # Not queued for retry: the reply is a live conversation turn, and a note
        # that lands minutes later would answer a thread that has moved on. The
        # caller gets ok=False and can tell the user in the thread.
        logger.warning(
            "task_comment_write_failed",
            task_id=task_id,
            envelope_error=status.get("envelope_error"),
            rejected=list((status.get("rejected") or {}).values()),
            retryable=bool(status.get("retryable") or status.get("rejected_retryable")),
        )
    return {"ok": bool(status["ok"]), "task_id": task_id}
