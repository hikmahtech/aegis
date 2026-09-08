"""Overview routes — unify dashboard + brief + status + system info for the UI."""

from __future__ import annotations

import os
import time
from typing import Any

from fastapi import APIRouter, Depends, Request

from aegis.api.auth import verify_auth

router = APIRouter(prefix="/api", dependencies=[Depends(verify_auth)])

_STARTED_AT = time.time()


@router.get("/overview/brief")
async def get_brief(request: Request) -> dict[str, Any]:
    """Consolidated daily-brief counts (the daily-brief summary)."""
    pool = request.app.state.db_pool
    pending_interactions = await pool.fetchval(
        "SELECT count(*) FROM interactions WHERE status = 'pending'"
    )
    # Both numbers come from the problem hub, which is the thing that knows.
    # This used to count `AlertInvestigationFlow` runs and call them alerts —
    # but the hub starts a flow only for a NEW or returning problem, so an
    # alert that deduped onto an open problem, or that a deploy window
    # suppressed, started no flow and went uncounted. The tile read zero while
    # a service was flapping.
    open_problems = (
        await pool.fetchval(
            "SELECT count(*) FROM problems WHERE closed_at IS NULL "
            "AND status NOT IN ('resolved', 'suppressed') "
            "AND (muted_until IS NULL OR muted_until <= now())"
        )
        or 0
    )
    occurrences_24h = (
        await pool.fetchval(
            "SELECT count(*) FROM problem_events WHERE kind = 'occurrence' "
            "AND occurred_at > now() - interval '24 hours'"
        )
        or 0
    )
    return {
        "pending_interactions": pending_interactions or 0,
        "open_problems": open_problems,
        "occurrences_24h": occurrences_24h,
    }


@router.get("/overview/status")
async def get_status(request: Request) -> dict[str, Any]:
    """System status — last runs of each registered workflow."""
    pool = request.app.state.db_pool
    rows = await pool.fetch(
        "SELECT workflow_type, max(completed_at) AS last_run "
        "FROM workflow_runs WHERE completed_at IS NOT NULL GROUP BY workflow_type "
        "ORDER BY workflow_type"
    )
    return {
        "last_workflow_runs": [
            {"workflow_type": r["workflow_type"], "last_run": r["last_run"]} for r in rows
        ],
    }


@router.get("/system/info")
async def get_system_info() -> dict[str, Any]:
    """Basic system info — version, build SHA, uptime."""
    return {
        "version": "0.1.0",
        "git_sha": os.environ.get("GIT_SHA", "dev"),
        "uptime_seconds": int(time.time() - _STARTED_AT),
    }
