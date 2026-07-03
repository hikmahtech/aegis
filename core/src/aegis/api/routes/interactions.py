"""Interactions API.

GET /api/interactions — list (paginated, filterable)
GET /api/interactions/{id} — single
POST /api/interactions/{id}/resolve — resolve from UI or chat

The resolve endpoint is the single choke-point for every human response.
It updates the DB row AND sends a Temporal signal to the InteractionFlow
workflow. If the row is already resolved (idempotent re-entry), no signal
is sent.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from temporalio.client import Client

from aegis.api.auth import verify_auth
from aegis.api.deps import get_settings
from aegis.config import Settings

logger = structlog.get_logger()

router = APIRouter(
    prefix="/api/interactions",
    tags=["interactions"],
    dependencies=[Depends(verify_auth)],
)


async def get_workflow_client(settings: Settings = Depends(get_settings)) -> Client:
    """Temporal client dependency. Tests override this via app.dependency_overrides."""
    return await Client.connect(settings.temporal_host)


class ResolveBody(BaseModel):
    response: dict[str, Any]


class ResolveResponse(BaseModel):
    interaction_id: str
    status: str
    already_resolved: bool


@router.post("/{interaction_id}/resolve", response_model=ResolveResponse)
async def resolve_interaction(
    interaction_id: UUID,
    body: ResolveBody,
    request: Request,
    temporal: Client = Depends(get_workflow_client),
) -> ResolveResponse:
    pool = request.app.state.db_pool

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT flow_run_id, status, agent_id, prompt FROM interactions WHERE id = $1",
            interaction_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="interaction_not_found")

        if row["status"] != "pending":
            logger.info(
                "interaction_already_resolved",
                interaction_id=str(interaction_id),
                status=row["status"],
            )
            return ResolveResponse(
                interaction_id=str(interaction_id),
                status=row["status"],
                already_resolved=True,
            )

        # Guard with `AND status='pending'` even though the SELECT above just
        # observed pending — a concurrent /resolve POST (from a duplicate
        # chat callback tap) or the workflow's own apply_interaction_timeout
        # can flip the row between the SELECT and the UPDATE. RETURNING id
        # gives us the row-count to decide whether the signal needs to fire.
        updated = await conn.fetchrow(
            "UPDATE interactions "
            "SET status = 'resolved', response = $2, resolved_at = now() "
            "WHERE id = $1 AND status = 'pending' RETURNING id",
            interaction_id,
            body.response,
        )

    if updated is None:
        logger.info(
            "interaction_resolve_race_lost",
            interaction_id=str(interaction_id),
        )
        return ResolveResponse(
            interaction_id=str(interaction_id),
            status="resolved",
            already_resolved=True,
        )

    # Learning loop (Phase 4): a human correction (a reason/note in the response)
    # becomes a durable lesson for this agent, surfaced in its next chat prompt.
    from aegis.services.memory import record_correction_from_interaction

    await record_correction_from_interaction(
        pool, row["agent_id"], row["prompt"], body.response
    )

    handle = temporal.get_workflow_handle(row["flow_run_id"])
    try:
        await handle.signal("submit_response", body.response)
    except Exception as exc:
        logger.warning(
            "interaction_signal_failed",
            interaction_id=str(interaction_id),
            flow_run_id=row["flow_run_id"],
            error=str(exc),
        )

    return ResolveResponse(
        interaction_id=str(interaction_id),
        status="resolved",
        already_resolved=False,
    )


@router.get("/{interaction_id}")
async def get_interaction(interaction_id: UUID, request: Request):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, flow_run_id, agent_id, kind, origin, prompt, options, "
            "status, response, timeout_policy, timeout_at, telegram_message_id, "
            "created_at, resolved_at "
            "FROM interactions WHERE id = $1",
            interaction_id,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="interaction_not_found")
    return dict(row)


@router.get("")
async def list_interactions(
    request: Request,
    agent_id: str | None = None,
    status: Literal["pending", "resolved", "archived"] | None = None,
    origin: str | None = None,
    limit: int = 50,
):
    pool = request.app.state.db_pool
    clauses: list[str] = []
    args: list[Any] = []
    if agent_id:
        args.append(agent_id)
        clauses.append(f"agent_id = ${len(args)}")
    if status:
        args.append(status)
        clauses.append(f"status = ${len(args)}")
    if origin:
        origins = [o for o in origin.split(",") if o]
        if origins:
            args.append(origins)
            clauses.append(f"origin = ANY(${len(args)}::text[])")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    args.append(min(max(limit, 1), 500))
    sql = (
        "SELECT id, agent_id, kind, origin, prompt, options, status, created_at, resolved_at "
        f"FROM interactions {where} "
        f"ORDER BY created_at DESC LIMIT ${len(args)}"
    )
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return [dict(r) for r in rows]
