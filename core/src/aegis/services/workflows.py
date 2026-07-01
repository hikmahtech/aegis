"""Shared workflow trigger service for chat and admin routes."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import structlog

logger = structlog.get_logger()

TRIGGERABLE_WORKFLOWS: dict[str, dict[str, str]] = {
    # Worker polls "aegis-main" (see worker/src/aegis_worker/__main__.py:TASK_QUEUE).
    # Triggers landing on any other queue are orphans nobody picks up.
    "daily_briefing": {"workflow": "DailyBriefingFlow", "task_queue": "aegis-main"},
    "weekly_review": {"workflow": "WeeklyReviewFlow", "task_queue": "aegis-main"},
}


async def trigger_workflow(
    client: Any,
    workflow_type: str,
    params: dict | None = None,
) -> dict[str, Any]:
    """Start a Temporal workflow by type name. Returns {workflow_id, workflow_type, status} or {error}."""
    config = TRIGGERABLE_WORKFLOWS.get(workflow_type)
    if not config:
        return {
            "error": f"Unknown workflow type: {workflow_type}. Valid: {list(TRIGGERABLE_WORKFLOWS.keys())}"
        }

    workflow_id = f"chat-{workflow_type}-{uuid4().hex[:8]}"
    try:
        handle = await client.start_workflow(
            config["workflow"],
            arg=params or {},
            id=workflow_id,
            task_queue=config["task_queue"],
        )
        logger.info(
            "workflow_triggered_from_chat", workflow_type=workflow_type, workflow_id=handle.id
        )
        return {"workflow_id": handle.id, "workflow_type": workflow_type, "status": "started"}
    except Exception as exc:
        logger.error("workflow_trigger_failed", workflow_type=workflow_type, error=str(exc))
        return {"error": f"Failed to start {workflow_type}: {str(exc)}"}
