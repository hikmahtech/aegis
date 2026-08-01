"""Admin CRUD for the household/asset registry (`life.assets`, migration 019).

Mirrors expiring_items_admin.py / people_admin.py: thin handlers over
services/assets.py, audit-logged mutations, auth on the whole router.
Consumed by the admin panel's Assets page.

No provisioning/status endpoints — unlike infra_admin.py, which this
generalises, an asset is data, not something we can SSH into.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from aegis.api.auth import verify_auth
from aegis.observability import log_audit
from aegis.services import assets as assets_service

router = APIRouter(prefix="/api/admin/assets", dependencies=[Depends(verify_auth)])


class AssetCreate(BaseModel):
    name: str
    # Open string: car | appliance | hvac | plumbing | electronics | ...
    # Lowercased by the service.
    kind: str
    # Optional — derived from `name` and de-duplicated by the service when
    # omitted. Immutable afterwards (it anchors `asset:<slug>` tags).
    slug: str | None = None
    purchase_date: date | None = None
    warranty_until: date | None = None
    # Set BOTH to have the asset mirrored into the expiry radar as an
    # `asset_service` item due `last_serviced_at + service_interval_days`.
    service_interval_days: int | None = None
    last_serviced_at: date | None = None
    location: str | None = None
    notes: str | None = None
    metadata: dict[str, Any] = {}


class AssetUpdate(BaseModel):
    name: str | None = None
    kind: str | None = None
    purchase_date: date | None = None
    warranty_until: date | None = None
    # Explicit null on either of these two CLEARS it (and removes the service
    # reminder) — see services/assets.update_asset.
    service_interval_days: int | None = None
    last_serviced_at: date | None = None
    location: str | None = None
    notes: str | None = None
    metadata: dict[str, Any] | None = None


@router.get("")
async def list_assets(request: Request, kind: str | None = None) -> list[dict]:
    pool = request.app.state.db_pool
    return await assets_service.list_assets(pool, kind)


@router.get("/{asset_id}")
async def get_asset(request: Request, asset_id: UUID) -> dict:
    pool = request.app.state.db_pool
    row = await assets_service.get_asset(pool, asset_id)
    if not row:
        raise HTTPException(404, "Asset not found")
    return row


@router.post("", status_code=201)
async def create_asset(request: Request, body: AssetCreate) -> dict:
    pool = request.app.state.db_pool
    try:
        row = await assets_service.create_asset(pool, body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await log_audit(
        pool,
        actor="api:assets_admin",
        action="asset_created",
        target_type="asset",
        target_id=str(row["id"]),
        details={"slug": row["slug"], "kind": row["kind"]},
    )
    return row


@router.put("/{asset_id}")
async def update_asset(request: Request, asset_id: UUID, body: AssetUpdate) -> dict:
    pool = request.app.state.db_pool
    data = body.model_dump(exclude_unset=True)
    try:
        row = await assets_service.update_asset(pool, asset_id, data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not row:
        raise HTTPException(404, "Asset not found")
    await log_audit(
        pool,
        actor="api:assets_admin",
        action="asset_updated",
        target_type="asset",
        target_id=str(asset_id),
        details={"fields": list(data.keys())},
    )
    return row


@router.delete("/{asset_id}", status_code=204)
async def delete_asset(request: Request, asset_id: UUID) -> None:
    pool = request.app.state.db_pool
    deleted = await assets_service.delete_asset(pool, asset_id)
    if not deleted:
        raise HTTPException(404, "Asset not found")
    await log_audit(
        pool,
        actor="api:assets_admin",
        action="asset_deleted",
        target_type="asset",
        target_id=str(asset_id),
    )
