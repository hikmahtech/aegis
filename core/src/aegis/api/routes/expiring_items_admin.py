"""Admin CRUD for the expiry radar registry (`life.expiring_items`, migration 018).

Mirrors people_admin.py / resources.py: thin handlers over
services/expiring_items.py, audit-logged mutations, auth on the whole router.
Consumed by the admin panel's Expiry page.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from aegis.api.auth import verify_auth
from aegis.observability import log_audit
from aegis.services import expiring_items as items_service

router = APIRouter(prefix="/api/admin/expiring-items", dependencies=[Depends(verify_auth)])


class ExpiringItemCreate(BaseModel):
    # Open string: passport | visa | licence | insurance | warranty |
    # medication | domain | ... Lowercased by the service.
    kind: str
    title: str
    expires_on: date
    # Days-before-expiry to warn at. Normalised (deduped, descending) by the
    # service; omitted means the service default {30, 7, 1}.
    lead_days: list[int] | None = None
    person_id: UUID | None = None
    asset_id: UUID | None = None
    notes: str | None = None
    metadata: dict[str, Any] = {}


class ExpiringItemUpdate(BaseModel):
    kind: str | None = None
    title: str | None = None
    expires_on: date | None = None
    lead_days: list[int] | None = None
    person_id: UUID | None = None
    asset_id: UUID | None = None
    notes: str | None = None
    metadata: dict[str, Any] | None = None


@router.get("")
async def list_expiring_items(
    request: Request, kind: str | None = None, due_within: int | None = None
) -> list[dict]:
    """All items, or those of one `kind`, or those due within `due_within` days.

    `due_within` includes already-expired items (negative `days_left`) — see
    the service docstring.
    """
    pool = request.app.state.db_pool
    if due_within is not None:
        return await items_service.due_within(pool, due_within)
    return await items_service.list_expiring_items(pool, kind)


@router.get("/{item_id}")
async def get_expiring_item(request: Request, item_id: UUID) -> dict:
    pool = request.app.state.db_pool
    row = await items_service.get_expiring_item(pool, item_id)
    if not row:
        raise HTTPException(404, "Expiring item not found")
    return row


@router.post("", status_code=201)
async def create_expiring_item(request: Request, body: ExpiringItemCreate) -> dict:
    pool = request.app.state.db_pool
    try:
        row = await items_service.create_expiring_item(pool, body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await log_audit(
        pool,
        actor="api:expiring_items_admin",
        action="expiring_item_created",
        target_type="expiring_item",
        target_id=str(row["id"]),
        details={"kind": row["kind"], "title": row["title"]},
    )
    return row


@router.put("/{item_id}")
async def update_expiring_item(request: Request, item_id: UUID, body: ExpiringItemUpdate) -> dict:
    pool = request.app.state.db_pool
    data = body.model_dump(exclude_unset=True)
    try:
        row = await items_service.update_expiring_item(pool, item_id, data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not row:
        raise HTTPException(404, "Expiring item not found")
    await log_audit(
        pool,
        actor="api:expiring_items_admin",
        action="expiring_item_updated",
        target_type="expiring_item",
        target_id=str(item_id),
        details={"fields": list(data.keys())},
    )
    return row


@router.delete("/{item_id}", status_code=204)
async def delete_expiring_item(request: Request, item_id: UUID) -> None:
    pool = request.app.state.db_pool
    deleted = await items_service.delete_expiring_item(pool, item_id)
    if not deleted:
        raise HTTPException(404, "Expiring item not found")
    await log_audit(
        pool,
        actor="api:expiring_items_admin",
        action="expiring_item_deleted",
        target_type="expiring_item",
        target_id=str(item_id),
    )
