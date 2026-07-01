"""Admin endpoints for Todoist sync + outbox visibility.

Todoist is the canonical GTD task store; this surface answers "is the sync
loop healthy and has any write been lost?" — most importantly exposing
todoist_outbox rows stuck in status='failed', which previously had no reader
anywhere (a permanently failed write silently lost the captured task).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from aegis.api.auth import verify_auth
from aegis.api.deps import get_settings
from aegis.config import Settings

router = APIRouter(
    prefix="/api/admin/todoist",
    tags=["todoist"],
    dependencies=[Depends(verify_auth)],
)


@router.get("/config")
async def get_todoist_config(
    request: Request, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """Todoist config status: api_key set + source, and the managed project ids."""
    from aegis.services.todoist_config import todoist_config_status

    return await todoist_config_status(request.app.state.db_pool, settings)


@router.put("/config")
async def put_todoist_config(
    request: Request, body: dict[str, Any], settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """Set the Todoist API key (omitted → keep) and/or the managed project ids."""
    from aegis.services.todoist_config import save_todoist_config, todoist_config_status

    pool = request.app.state.db_pool
    api_key = body.get("api_key") if "api_key" in body else None
    projects = body.get("projects") if "projects" in body else None
    await save_todoist_config(pool, settings, api_key=api_key, projects=projects)
    return await todoist_config_status(pool, settings)


@router.get("/gtd-rules")
async def get_gtd_rules_route(request: Request) -> dict[str, Any]:
    """The GTD clarify taxonomy (source-tag → assignee / contexts / skip-inbox)."""
    from aegis.services.gtd_rules import SOURCE_TAGS, get_gtd_rules

    return {"source_tags": SOURCE_TAGS, **await get_gtd_rules(request.app.state.db_pool)}


@router.put("/gtd-rules")
async def put_gtd_rules_route(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Save the GTD taxonomy (assignee/contexts/skip_inbox maps); returns merged."""
    from aegis.services.gtd_rules import SOURCE_TAGS, save_gtd_rules

    return {"source_tags": SOURCE_TAGS, **await save_gtd_rules(request.app.state.db_pool, body)}


@router.get("/state")
async def todoist_state(request: Request) -> dict:
    """Sync watermarks, projection counts, and outbox health in one call."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        sync = await conn.fetchrow(
            "SELECT key, last_full_sync_at, last_incremental_at "
            "FROM todoist_sync_state WHERE key='main'"
        )
        outbox_counts = await conn.fetch(
            "SELECT status, count(*) AS n FROM todoist_outbox GROUP BY status"
        )
        oldest_pending = await conn.fetchval(
            "SELECT EXTRACT(epoch FROM now() - min(created_at))::int "
            "FROM todoist_outbox WHERE status='pending'"
        )
        failed_recent = await conn.fetch(
            "SELECT id, temp_id, command->>'type' AS command_type, "
            "attempt_count, last_attempt_at, created_at "
            "FROM todoist_outbox WHERE status='failed' "
            "ORDER BY created_at DESC LIMIT 50"
        )
        managed = await conn.fetchval(
            "SELECT value FROM settings WHERE key='todoist_managed_project_ids'"
        )
        open_tasks = await conn.fetchval(
            "SELECT count(*) FROM todoist_tasks WHERE NOT is_completed"
        )
        completed_7d = await conn.fetchval(
            "SELECT count(*) FROM todoist_tasks "
            "WHERE is_completed AND completed_at > now() - interval '7 days'"
        )
        unclarified = await conn.fetchval(
            "SELECT count(*) FROM todoist_tasks "
            "WHERE NOT is_completed AND source_tag IS NOT NULL "
            "AND last_clarified_at IS NULL"
        )
    return {
        "sync": dict(sync) if sync else None,
        "outbox": {
            "counts": {r["status"]: r["n"] for r in outbox_counts},
            "oldest_pending_age_seconds": oldest_pending,
            "failed_recent": [dict(r) for r in failed_recent],
        },
        "tasks": {
            "open": int(open_tasks or 0),
            "completed_7d": int(completed_7d or 0),
            "pending_clarify": int(unclarified or 0),
        },
        "managed_projects": managed if isinstance(managed, dict) else None,
    }
