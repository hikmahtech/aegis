"""Phase 5 polish: POST /api/admin/capture for Telegram /capture command.

A tiny wrapper around the same _capture_to_inbox_impl helper that the
capture_to_inbox chat tool uses. Lets the Telegram bot (which has no
direct DB access) drop a task into the Todoist Inbox by HTTP.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from aegis.api.auth import verify_auth

logger = structlog.get_logger()


router = APIRouter(
    prefix="/api/admin",
    tags=["capture"],
    dependencies=[Depends(verify_auth)],
)


class CaptureRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)
    source: str = Field(default="telegram", pattern=r"^[a-z_]{1,32}$")
    description: str | None = Field(default=None, max_length=8000)
    # Optional explicit external_id (for idempotent re-tries from the bot).
    # If omitted, a hash of (source + text) is used.
    external_id: str | None = Field(default=None, max_length=128)


class CaptureResponse(BaseModel):
    task_ref: str | None
    source_tag: str
    external_id: str


@router.post("/capture", response_model=CaptureResponse)
async def capture(
    body: CaptureRequest,
    request: Request,
) -> CaptureResponse:
    """Drop a one-line task into the Todoist Inbox.

    Idempotent on (source_tag, external_id) via todoist_capture_idempotency.
    Returns the Todoist task id (or temp_id if outbox-queued) plus the
    source_tag used so the caller has the audit trail.
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
    )
