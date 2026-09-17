"""Shared workflow trigger service for chat and admin routes."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import structlog

from aegis.errors import error_text

logger = structlog.get_logger()

# Worker polls "aegis-main" (see worker/src/aegis_worker/__main__.py:TASK_QUEUE).
# Triggers landing on any other queue are orphans nobody picks up.
TASK_QUEUE = "aegis-main"


async def workflow_owner(pool: Any, workflow_type: str) -> str | None:
    """The agent that owns `workflow_type`'s `activities` row — who a manual or
    chat start runs as when the caller names nobody (#579). Without it the
    flow's dataclass default decides, and those defaults name the example
    agents. An active row wins, then the lowest slug; None when there is no
    row or the read fails."""
    if pool is None:
        return None
    try:
        owner = await pool.fetchval(
            "SELECT agent_id FROM activities WHERE workflow_type = $1 "
            "ORDER BY active DESC, slug LIMIT 1",
            workflow_type,
        )
    except Exception as exc:  # noqa: BLE001 — a start never fails on its owner lookup
        logger.warning(
            "workflow_owner_lookup_failed",
            workflow_type=workflow_type,
            error=error_text(exc),
        )
        return None
    return owner if isinstance(owner, str) and owner else None


def with_owner(params: dict | None, owner: str | None) -> dict:
    """`params` with `agent_id` filled from `owner` when the caller named none."""
    out = dict(params or {})
    if owner and not out.get("agent_id"):
        out["agent_id"] = owner
    return out


async def trigger_workflow(
    client: Any,
    pool: Any,
    workflow_type: str,
    params: dict | None = None,
) -> dict[str, Any]:
    """Start a Temporal workflow by type name. Returns {workflow_id, workflow_type, status} or {error}.

    Valid workflow_type values are whatever is scheduled in `activities`
    (see `_exec_create_schedule` in chat.py for the same lookup pattern).
    """
    valid_rows = await pool.fetch("SELECT DISTINCT workflow_type FROM activities ORDER BY 1")
    valid = [r["workflow_type"] for r in valid_rows]
    if workflow_type not in valid:
        return {"error": f"Unknown workflow type: {workflow_type}. Valid: {valid}"}

    workflow_id = f"chat-{workflow_type}-{uuid4().hex[:8]}"
    arg = with_owner(params, await workflow_owner(pool, workflow_type))
    try:
        handle = await client.start_workflow(
            workflow_type,
            arg=arg,
            id=workflow_id,
            task_queue=TASK_QUEUE,
        )
        logger.info(
            "workflow_triggered_from_chat", workflow_type=workflow_type, workflow_id=handle.id
        )
        return {"workflow_id": handle.id, "workflow_type": workflow_type, "status": "started"}
    except Exception as exc:
        logger.error("workflow_trigger_failed", workflow_type=workflow_type, error=error_text(exc, 500))
        return {"error": f"Failed to start {workflow_type}: {error_text(exc, 500)}"}
