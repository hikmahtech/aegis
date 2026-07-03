"""Admin CRUD for ingestion channels (email / rss / raindrop).

Channels are DB-owned: `config/seed/channels.yaml` only inserts starter rows
on first boot (see seed.py::_load_channels); everything afterwards is managed
here and from the admin panel's Channels page.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from aegis.api.auth import verify_auth

router = APIRouter(prefix="/api/admin/channels", dependencies=[Depends(verify_auth)])

CHANNEL_KINDS = ("email", "rss", "raindrop")

_COLS = "id, kind, identifier, config, active, created_at"


class ChannelCreate(BaseModel):
    kind: str
    identifier: str
    config: dict[str, Any] = {}
    active: bool = True


class ChannelUpdate(BaseModel):
    identifier: str | None = None
    config: dict[str, Any] | None = None
    active: bool | None = None


@router.get("")
async def list_channels(request: Request, kind: str | None = None) -> list[dict]:
    pool = request.app.state.db_pool
    if kind:
        rows = await pool.fetch(
            f"SELECT {_COLS} FROM channels WHERE kind = $1 ORDER BY kind, identifier",
            kind,
        )
    else:
        rows = await pool.fetch(f"SELECT {_COLS} FROM channels ORDER BY kind, identifier")
    return [dict(r) for r in rows]


@router.post("", status_code=201)
async def create_channel(request: Request, body: ChannelCreate) -> dict:
    if body.kind not in CHANNEL_KINDS:
        raise HTTPException(400, f"Unknown channel kind {body.kind!r}; expected one of {list(CHANNEL_KINDS)}")
    identifier = body.identifier.strip()
    if not identifier:
        raise HTTPException(400, "identifier must not be empty")
    pool = request.app.state.db_pool
    try:
        row = await pool.fetchrow(
            f"INSERT INTO channels (kind, identifier, config, active) "
            f"VALUES ($1, $2, $3, $4) RETURNING {_COLS}",
            body.kind,
            identifier,
            body.config,
            body.active,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, f"A {body.kind} channel with identifier {identifier!r} already exists") from None
    return dict(row)


@router.patch("/{channel_id}")
async def update_channel(request: Request, channel_id: UUID, body: ChannelUpdate) -> dict:
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, "No fields to update")
    if "identifier" in fields and not (fields["identifier"] or "").strip():
        raise HTTPException(400, "identifier must not be empty")
    pool = request.app.state.db_pool
    try:
        row = await pool.fetchrow(
            f"""
            UPDATE channels SET
                identifier = COALESCE($2, identifier),
                config = COALESCE($3, config),
                active = COALESCE($4, active)
            WHERE id = $1
            RETURNING {_COLS}
            """,
            channel_id,
            fields["identifier"].strip() if "identifier" in fields else None,
            fields.get("config"),
            fields.get("active"),
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, f"A channel with identifier {fields['identifier']!r} already exists for this kind") from None
    if not row:
        raise HTTPException(404, "Channel not found")
    return dict(row)


@router.delete("/{channel_id}", status_code=204)
async def delete_channel(request: Request, channel_id: UUID) -> None:
    pool = request.app.state.db_pool
    result = await pool.execute("DELETE FROM channels WHERE id = $1", channel_id)
    if result == "DELETE 0":
        raise HTTPException(404, "Channel not found")
