"""Shared workflow trigger service for chat and admin routes."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import structlog

logger = structlog.get_logger()

# Worker polls "aegis-main" (see worker/src/aegis_worker/__main__.py:TASK_QUEUE).
# Triggers landing on any other queue are orphans nobody picks up.
TASK_QUEUE = "aegis-main"


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
    try:
        handle = await client.start_workflow(
            workflow_type,
            arg=params or {},
            id=workflow_id,
            task_queue=TASK_QUEUE,
        )
        logger.info(
            "workflow_triggered_from_chat", workflow_type=workflow_type, workflow_id=handle.id
        )
        return {"workflow_id": handle.id, "workflow_type": workflow_type, "status": "started"}
    except Exception as exc:
        logger.error("workflow_trigger_failed", workflow_type=workflow_type, error=str(exc))
        return {"error": f"Failed to start {workflow_type}: {str(exc)}"}
