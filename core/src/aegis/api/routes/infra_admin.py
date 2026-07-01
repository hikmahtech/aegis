"""Admin CRUD + provisioning for the infra registry table.

Mirrors the resources.py CRUD pattern. Action/inspection endpoints (service
listing, pod logs, argocd sync, etc.) live in routes/infra.py — this file is
only the infra *registry* (create/edit/delete host entries + trigger
provisioning).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from aegis.api.auth import verify_auth
from aegis.observability import log_audit
from aegis.services import infra as infra_service

router = APIRouter(prefix="/api/admin/infra", dependencies=[Depends(verify_auth)])


class SetupFile(BaseModel):
    path: str
    content: str = ""
    mode: str | None = None


class InfraCreate(BaseModel):
    name: str
    slug: str | None = None
    kind: str = "ssh_host"
    host: str | None = None
    ssh_user: str | None = None
    ssh_port: int = 22
    ssh_key_ref: str | None = None
    docker_context: str | None = None
    hosts_aegis: bool = False
    setup_files: list[SetupFile] = []
    setup_command: str | None = None
    metadata: dict[str, Any] = {}


class InfraUpdate(BaseModel):
    name: str | None = None
    kind: str | None = None
    host: str | None = None
    ssh_user: str | None = None
    ssh_port: int | None = None
    ssh_key_ref: str | None = None
    docker_context: str | None = None
    hosts_aegis: bool | None = None
    setup_files: list[SetupFile] | None = None
    setup_command: str | None = None
    metadata: dict[str, Any] | None = None


def _dump_setup_files(files: list[SetupFile] | None) -> list[dict] | None:
    if files is None:
        return None
    return [f.model_dump() for f in files]


@router.get("")
async def list_infra(request: Request) -> list[dict]:
    pool = request.app.state.db_pool
    return await infra_service.list_infra(pool)


@router.get("/{infra_id}")
async def get_infra(request: Request, infra_id: UUID) -> dict:
    pool = request.app.state.db_pool
    row = await infra_service.get_infra(pool, infra_id)
    if not row:
        raise HTTPException(404, "Infra entry not found")
    return row


@router.post("", status_code=201)
async def create_infra(request: Request, body: InfraCreate) -> dict:
    pool = request.app.state.db_pool
    data = body.model_dump()
    data["setup_files"] = _dump_setup_files(body.setup_files)
    row = await infra_service.create_infra(pool, data)
    await log_audit(
        pool,
        actor="api:infra_admin",
        action="infra_created",
        target_type="infra",
        target_id=row["id"] if isinstance(row["id"], str) else str(row["id"]),
        details={"slug": row["slug"], "name": row["name"]},
    )
    return row


@router.put("/{infra_id}")
async def update_infra(request: Request, infra_id: UUID, body: InfraUpdate) -> dict:
    pool = request.app.state.db_pool
    data = body.model_dump(exclude_unset=True)
    if "setup_files" in data:
        data["setup_files"] = _dump_setup_files(body.setup_files)
    row = await infra_service.update_infra(pool, infra_id, data)
    if not row:
        raise HTTPException(404, "Infra entry not found")
    await log_audit(
        pool,
        actor="api:infra_admin",
        action="infra_updated",
        target_type="infra",
        target_id=str(infra_id),
        details={"fields": list(data.keys())},
    )
    return row


@router.delete("/{infra_id}", status_code=204)
async def delete_infra(request: Request, infra_id: UUID) -> None:
    pool = request.app.state.db_pool
    deleted = await infra_service.delete_infra(pool, infra_id)
    if not deleted:
        raise HTTPException(404, "Infra entry not found")
    await log_audit(
        pool,
        actor="api:infra_admin",
        action="infra_deleted",
        target_type="infra",
        target_id=str(infra_id),
    )


@router.post("/{infra_id}/provision")
async def provision_infra(request: Request, infra_id: UUID) -> dict:
    pool = request.app.state.db_pool
    existing = await infra_service.get_infra(pool, infra_id)
    if not existing:
        raise HTTPException(404, "Infra entry not found")
    result = await infra_service.provision_infra(pool, infra_id)
    await log_audit(
        pool,
        actor="api:infra_admin",
        action="infra_provisioned",
        target_type="infra",
        target_id=str(infra_id),
        details={"status": result.get("status"), "error": result.get("last_error")},
    )
    return result
