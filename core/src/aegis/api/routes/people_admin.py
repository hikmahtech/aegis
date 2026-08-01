"""Admin CRUD for the people registry (`life.people`, migration 016).

Mirrors the resources.py / infra_admin.py CRUD pattern: thin handlers over
services/people.py, audit-logged mutations, auth on the whole router.
Consumed by the admin panel's People page.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from aegis.api.auth import verify_auth
from aegis.observability import log_audit
from aegis.services import people as people_service

router = APIRouter(prefix="/api/admin/people", dependencies=[Depends(verify_auth)])


class PersonCreate(BaseModel):
    name: str
    # Nicknames / other names / email addresses. Lowercased by the service.
    aliases: list[str] = []
    relationship: str | None = None
    # {"birthday": "1985-04-02", "anniversary": "2016-11-19"}
    key_dates: dict[str, Any] = {}
    notes: str | None = None
    last_contact: datetime | None = None
    metadata: dict[str, Any] = {}


class PersonUpdate(BaseModel):
    name: str | None = None
    aliases: list[str] | None = None
    relationship: str | None = None
    key_dates: dict[str, Any] | None = None
    notes: str | None = None
    last_contact: datetime | None = None
    metadata: dict[str, Any] | None = None


@router.get("")
async def list_people(request: Request, q: str | None = None) -> list[dict]:
    """All people, or — with `q` — those whose name OR alias matches it."""
    pool = request.app.state.db_pool
    if q:
        return await people_service.find_people(pool, q)
    return await people_service.list_people(pool)


@router.get("/{person_id}")
async def get_person(request: Request, person_id: UUID) -> dict:
    pool = request.app.state.db_pool
    row = await people_service.get_person(pool, person_id)
    if not row:
        raise HTTPException(404, "Person not found")
    return row


@router.post("", status_code=201)
async def create_person(request: Request, body: PersonCreate) -> dict:
    pool = request.app.state.db_pool
    try:
        row = await people_service.create_person(pool, body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await log_audit(
        pool,
        actor="api:people_admin",
        action="person_created",
        target_type="person",
        target_id=str(row["id"]),
        details={"name": row["name"]},
    )
    return row


@router.put("/{person_id}")
async def update_person(request: Request, person_id: UUID, body: PersonUpdate) -> dict:
    pool = request.app.state.db_pool
    data = body.model_dump(exclude_unset=True)
    try:
        row = await people_service.update_person(pool, person_id, data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not row:
        raise HTTPException(404, "Person not found")
    await log_audit(
        pool,
        actor="api:people_admin",
        action="person_updated",
        target_type="person",
        target_id=str(person_id),
        details={"fields": list(data.keys())},
    )
    return row


@router.delete("/{person_id}", status_code=204)
async def delete_person(request: Request, person_id: UUID) -> None:
    pool = request.app.state.db_pool
    deleted = await people_service.delete_person(pool, person_id)
    if not deleted:
        raise HTTPException(404, "Person not found")
    await log_audit(
        pool,
        actor="api:people_admin",
        action="person_deleted",
        target_type="person",
        target_id=str(person_id),
    )
