"""Admin CRUD for the v3 resources table (connectors, runbooks, repositories, etc.)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from aegis.api.auth import verify_auth

router = APIRouter(prefix="/api/admin/resources", dependencies=[Depends(verify_auth)])


class ResourceCreate(BaseModel):
    kind: str
    slug: str
    title: str
    url: str | None = None
    content: str | None = None
    tags: list[str] = []
    metadata: dict[str, Any] = {}
    infra_id: UUID | None = None


class ResourceUpdate(BaseModel):
    title: str | None = None
    url: str | None = None
    content: str | None = None
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None
    infra_id: UUID | None = None


@router.get("")
async def list_resources(request: Request, kind: str | None = None) -> list[dict]:
    pool = request.app.state.db_pool
    if kind:
        rows = await pool.fetch(
            "SELECT id, kind, slug, title, url, content, tags, metadata, infra_id, created_at, updated_at "
            "FROM resources WHERE kind = $1 ORDER BY kind, title",
            kind,
        )
    else:
        rows = await pool.fetch(
            "SELECT id, kind, slug, title, url, content, tags, metadata, infra_id, created_at, updated_at "
            "FROM resources ORDER BY kind, title"
        )
    return [dict(r) for r in rows]


@router.get("/{resource_id}")
async def get_resource(request: Request, resource_id: UUID) -> dict:
    pool = request.app.state.db_pool
    row = await pool.fetchrow(
        "SELECT id, kind, slug, title, url, content, tags, metadata, infra_id, created_at, updated_at "
        "FROM resources WHERE id = $1",
        resource_id,
    )
    if not row:
        raise HTTPException(404, "Resource not found")
    return dict(row)


@router.post("", status_code=201)
async def create_resource(request: Request, body: ResourceCreate) -> dict:
    pool = request.app.state.db_pool
    row = await pool.fetchrow(
        "INSERT INTO resources (kind, slug, title, url, content, tags, metadata, infra_id) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) "
        "RETURNING id, kind, slug, title, url, content, tags, metadata, infra_id, created_at, updated_at",
        body.kind,
        body.slug,
        body.title,
        body.url,
        body.content,
        body.tags,
        body.metadata,
        body.infra_id,
    )
    return dict(row)


@router.put("/{resource_id}")
async def update_resource(request: Request, resource_id: UUID, body: ResourceUpdate) -> dict:
    pool = request.app.state.db_pool
    existing = await pool.fetchrow("SELECT * FROM resources WHERE id = $1", resource_id)
    if not existing:
        raise HTTPException(404, "Resource not found")
    row = await pool.fetchrow(
        "UPDATE resources SET "
        "  title = COALESCE($2, title), "
        "  url = COALESCE($3, url), "
        "  content = COALESCE($4, content), "
        "  tags = COALESCE($5, tags), "
        "  metadata = COALESCE($6, metadata), "
        "  infra_id = COALESCE($7, infra_id), "
        "  updated_at = now() "
        "WHERE id = $1 "
        "RETURNING id, kind, slug, title, url, content, tags, metadata, infra_id, created_at, updated_at",
        resource_id,
        body.title,
        body.url,
        body.content,
        body.tags,
        body.metadata,
        body.infra_id,
    )
    return dict(row)


@router.delete("/{resource_id}", status_code=204)
async def delete_resource(request: Request, resource_id: UUID) -> None:
    pool = request.app.state.db_pool
    result = await pool.execute("DELETE FROM resources WHERE id = $1", resource_id)
    if result == "DELETE 0":
        raise HTTPException(404, "Resource not found")
