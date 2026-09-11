"""Admin CRUD for per-alert runbooks (the `runbooks` table, migration 044, #499).

Thin handlers over services/runbooks.py, audit-logged mutations, auth on the
whole router — the same shape as expiring_items_admin.py. `{name}` is an alert
name in any spelling ("NodeDown", "Dagster Pipeline Failure",
"dagster-pipeline-failure"); URL-encode it. Consumed by the admin panel's
Runbooks page and by anyone loading runbooks with curl.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from aegis.api.auth import verify_auth
from aegis.observability import log_audit
from aegis.services import runbooks as runbooks_service

router = APIRouter(prefix="/api/admin/runbooks", dependencies=[Depends(verify_auth)])

_ACTOR = "api:runbooks_admin"


class RunbookPut(BaseModel):
    body: str
    # A free-text note of who or what wrote it ("loader", "arshad"). The API has
    # one admin identity, so this is a label, not an authenticated user.
    updated_by: str | None = None


@router.get("")
async def list_runbooks(request: Request) -> list[dict]:
    """Every stored runbook, by name, without the bodies."""
    return await runbooks_service.list_runbooks(request.app.state.db_pool)


@router.get("/{name}")
async def get_runbook(request: Request, name: str) -> dict:
    row = await runbooks_service.get_runbook(request.app.state.db_pool, name)
    if not row:
        raise HTTPException(404, "No stored runbook for that alert name")
    return row


@router.put("/{name}")
async def put_runbook(request: Request, name: str, payload: RunbookPut) -> dict:
    """Create or replace the runbook for an alert name. 400 on a runbook that
    could never be served (blank, a stub, over the size cap, a name with no
    letters or digits)."""
    pool = request.app.state.db_pool
    try:
        row = await runbooks_service.put_runbook(
            pool, name, payload.body, updated_by=payload.updated_by or "admin"
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await log_audit(
        pool,
        actor=_ACTOR,
        action="runbook_saved",
        target_type="runbook",
        target_id=row["name_key"],
        details={"name": row["name"], "chars": len(row["body"]), "created": row["created"]},
    )
    return row


@router.delete("/{name}", status_code=204)
async def delete_runbook(request: Request, name: str) -> None:
    pool = request.app.state.db_pool
    if not await runbooks_service.delete_runbook(pool, name):
        raise HTTPException(404, "No stored runbook for that alert name")
    await log_audit(
        pool,
        actor=_ACTOR,
        action="runbook_deleted",
        target_type="runbook",
        target_id=runbooks_service.normalise_name(name),
    )
