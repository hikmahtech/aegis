"""Phase 5 polish: POST /api/admin/capture for chat /capture commands.

A tiny wrapper around the same _capture_to_inbox_impl helper that the
capture_to_inbox chat tool uses. Lets a chat bot (which has no
direct DB access) drop a task into the Todoist Inbox by HTTP.

`kind` picks the capture lane: `task` (default) is the Todoist Inbox path
above; `life_fact` files the text as a knowledge-store fact instead, for
things that are true about your life rather than things to do; `auto` (B3)
hands the text to the intent classifier and takes whichever lane it names,
for voice notes where the speaker never said which one they meant.
"""

from __future__ import annotations

from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from aegis.api.auth import verify_auth
from aegis.api.deps import get_knowledge_connector

logger = structlog.get_logger()


router = APIRouter(
    prefix="/api/admin",
    tags=["capture"],
    dependencies=[Depends(verify_auth)],
)


class CaptureRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)
    source: str = Field(default="chat", pattern=r"^[a-z_]{1,32}$")
    description: str | None = Field(default=None, max_length=8000)
    # Optional explicit external_id (for idempotent re-tries from the bot).
    # If omitted, a hash of (source + text) is used.
    external_id: str | None = Field(default=None, max_length=128)
    kind: Literal["task", "life_fact", "auto"] = "task"


class CaptureResponse(BaseModel):
    task_ref: str | None
    source_tag: str
    external_id: str
    content_id: str | None = None
    # The lane actually written to — always "task" or "life_fact", never
    # "auto". Callers (Slack ack, iOS Shortcut confirmation) echo it so the
    # human sees where a classified note landed.
    lane: str = "task"


@router.post("/capture", response_model=CaptureResponse)
async def capture(
    body: CaptureRequest,
    request: Request,
) -> CaptureResponse:
    """Drop a one-line task into the Todoist Inbox, or file a life fact.

    Idempotent on (source_tag, external_id) via todoist_capture_idempotency.
    Returns the Todoist task id (or temp_id if outbox-queued) plus the
    source_tag used so the caller has the audit trail.

    `kind="life_fact"` skips Todoist entirely and upserts the text into the
    knowledge store under a synthetic `aegis://life_fact/{external_id}` URL,
    so re-posting the same text is an idempotent no-op (`content_id` is a
    hash of that URL). `task_ref` is None on that lane, and text that ingests
    to nothing (whitespace-only) is a 422 rather than a phantom `content_id`.

    `kind="auto"` resolves the lane with the intent classifier first (see
    `services/capture_classify.py`); any classifier failure degrades to the
    task lane, so this request shape can never fail on account of the LLM.
    The resolved lane comes back as `lane`.
    """
    from aegis.services.chat import _capture_to_inbox_impl  # avoid circular import

    pool = request.app.state.db_pool
    source_tag = f"#{body.source}"
    if body.external_id:
        ext_id = body.external_id
    else:
        import hashlib
        ext_id = (
            f"{body.source}:"
            f"{hashlib.sha256(body.text.encode()).hexdigest()[:16]}"
        )

    lane = body.kind
    if lane == "auto":
        from aegis.services.capture_classify import classify_capture

        lane = (
            await classify_capture(
                pool=pool,
                llm=getattr(request.app.state, "llm", None),
                text=body.text,
                external_id=ext_id,
                source=body.source,
            )
        ).lane

    if lane == "life_fact":
        store = get_knowledge_connector(request)
        text = body.text.strip()
        raw_text = f"{text}\n\n{body.description}" if body.description else text
        result = await store.ingest_content(
            url=f"aegis://life_fact/{ext_id}",
            title=text[:200],
            source_type="life_fact",
            raw_text=raw_text,
            tags=["life_fact", body.source],
        )
        content_id = result.get("content_id")
        status = result.get("status")
        if status == "empty":
            # ingest_content early-returns without writing when the body chunks
            # to nothing (e.g. whitespace-only text, which pydantic's
            # min_length=1 lets through and the strip above empties). Returning
            # 200 + content_id here hands the caller an id for a row that does
            # not exist — reject instead.
            logger.warning(
                "capture_admin_life_fact_empty",
                source_tag=source_tag,
                external_id=ext_id[:32],
            )
            raise HTTPException(
                status_code=422,
                detail="life_fact text is empty after stripping — nothing ingested",
            )
        logger.info(
            "capture_admin_life_fact",
            source_tag=source_tag,
            external_id=ext_id[:32],
            content_id=content_id,
            status=status,
        )
        return CaptureResponse(
            task_ref=None,
            source_tag=source_tag,
            external_id=ext_id,
            content_id=content_id,
            lane="life_fact",
        )

    ref = await _capture_to_inbox_impl(
        pool=pool,
        source_tag=source_tag,
        external_id=ext_id,
        title=body.text.strip(),
        description=body.description,
    )
    if ref is None:
        logger.warning(
            "capture_admin_skipped",
            source_tag=source_tag,
            external_id=ext_id[:32],
        )
        raise HTTPException(
            status_code=503,
            detail="capture skipped — kill switch off, missing inbox, or no api key",
        )
    logger.info(
        "capture_admin_emitted",
        source_tag=source_tag,
        external_id=ext_id[:32],
        task_ref=ref,
    )
    return CaptureResponse(
        task_ref=ref,
        source_tag=source_tag,
        external_id=ext_id,
        lane="task",
    )
