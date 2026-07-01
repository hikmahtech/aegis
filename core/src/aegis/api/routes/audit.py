"""Audit log endpoints — browse the action audit trail."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from aegis.api.auth import verify_auth
from aegis.api.sql_filters import build_where

router = APIRouter(prefix="/api/audit", dependencies=[Depends(verify_auth)])


@router.get("")
async def list_audit_log(
    request: Request,
    actor: str | None = None,
    action: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Browse audit log entries."""
    pool = request.app.state.db_pool
    where, params = build_where(
        {
            "actor": actor,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
        }
    )
    idx = len(params) + 1
    params.extend([limit, offset])
    rows = await pool.fetch(
        f"SELECT * FROM audit_log{where} ORDER BY created_at DESC LIMIT ${idx} OFFSET ${idx + 1}",
        *params,
    )
    return [dict(r) for r in rows]
