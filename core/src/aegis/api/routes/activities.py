"""Admin: scheduled-flow (activities) config — list + edit. Powers /admin/flows.

config / schedule_cron / active are DB-owned (the seed yaml is only initial
defaults — see seed._load_activities), so edits here are durable. schedule_sync
(worker, every ~300s) reconciles the Temporal schedule from the activities table,
so changes take effect within a few minutes with no deploy.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from aegis.api.auth import verify_auth

router = APIRouter(prefix="/api/admin/activities", dependencies=[Depends(verify_auth)])


@router.get("")
async def list_activities(request: Request) -> list[dict[str, Any]]:
    pool = request.app.state.db_pool
    rows = await pool.fetch(
        """
        SELECT a.slug, a.workflow_type, a.agent_id, a.schedule_cron, a.active,
               a.config, a.updated_at,
               (SELECT max(started_at) FROM workflow_runs r
                WHERE r.workflow_type = a.workflow_type) AS last_run
        FROM activities a
        ORDER BY a.agent_id, a.slug
        """
    )
    return [dict(r) for r in rows]


class ActivityPatch(BaseModel):
    active: bool | None = None
    schedule_cron: str | None = None
    config: dict | None = None


@router.patch("/{slug}")
async def update_activity(slug: str, body: ActivityPatch, request: Request) -> dict[str, Any]:
    pool = request.app.state.db_pool
    sets: list[str] = []
    args: list[Any] = []
    if body.active is not None:
        args.append(body.active)
        sets.append(f"active = ${len(args)}")
    if body.schedule_cron is not None:
        args.append(body.schedule_cron)
        sets.append(f"schedule_cron = ${len(args)}")
    if body.config is not None:
        args.append(body.config)
        sets.append(f"config = ${len(args)}")
    if not sets:
        raise HTTPException(status_code=400, detail="no fields to update")
    args.append(slug)
    row = await pool.fetchrow(
        f"UPDATE activities SET {', '.join(sets)}, updated_at = now() "
        f"WHERE slug = ${len(args)} "
        "RETURNING slug, workflow_type, agent_id, schedule_cron, active, config",
        *args,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"activity not found: {slug}")
    return dict(row)
