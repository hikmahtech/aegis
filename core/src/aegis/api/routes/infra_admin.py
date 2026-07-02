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
from aegis.api.deps import get_settings
from aegis.config import Settings
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
    # Write-only secrets — encrypted into infra.credentials, never returned
    # (responses carry has_ssh_key / has_kubeconfig booleans instead).
    ssh_private_key: str | None = None
    kubeconfig: str | None = None
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
    # Write-only; blank/omitted keeps the stored secret (slack_config convention).
    ssh_private_key: str | None = None
    kubeconfig: str | None = None
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
async def create_infra(
    request: Request, body: InfraCreate, settings: Settings = Depends(get_settings)
) -> dict:
    pool = request.app.state.db_pool
    data = body.model_dump()
    data["setup_files"] = _dump_setup_files(body.setup_files)
    row = await infra_service.create_infra(pool, data, settings.secret_key)
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
async def update_infra(
    request: Request, infra_id: UUID, body: InfraUpdate, settings: Settings = Depends(get_settings)
) -> dict:
    pool = request.app.state.db_pool
    data = body.model_dump(exclude_unset=True)
    if "setup_files" in data:
        data["setup_files"] = _dump_setup_files(body.setup_files)
    row = await infra_service.update_infra(pool, infra_id, data, settings.secret_key)
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


# ── k8s ops (kind=k8s entries; kubectl against the stored kubeconfig) ───────


def _k8s_result(result: dict) -> dict:
    if not result.get("ok"):
        raise HTTPException(result.get("status_code", 502), result.get("error", "k8s op failed"))
    return result


@router.get("/{infra_id}/k8s/pods")
async def k8s_pods(
    request: Request,
    infra_id: UUID,
    namespace: str = "default",
    settings: Settings = Depends(get_settings),
) -> dict:
    pool = request.app.state.db_pool
    return _k8s_result(
        await infra_service.k8s_list_pods(pool, infra_id, settings.secret_key, namespace)
    )


@router.get("/{infra_id}/k8s/deployments")
async def k8s_deployments(
    request: Request,
    infra_id: UUID,
    namespace: str = "default",
    settings: Settings = Depends(get_settings),
) -> dict:
    pool = request.app.state.db_pool
    return _k8s_result(
        await infra_service.k8s_list_deployments(pool, infra_id, settings.secret_key, namespace)
    )


@router.get("/{infra_id}/k8s/pods/{namespace}/{pod}/logs")
async def k8s_pod_logs(
    request: Request,
    infra_id: UUID,
    namespace: str,
    pod: str,
    tail: int = 200,
    settings: Settings = Depends(get_settings),
) -> dict:
    pool = request.app.state.db_pool
    return _k8s_result(
        await infra_service.k8s_pod_logs(pool, infra_id, settings.secret_key, namespace, pod, tail)
    )


@router.post("/{infra_id}/k8s/deployments/{namespace}/{name}/restart")
async def k8s_restart_deployment(
    request: Request,
    infra_id: UUID,
    namespace: str,
    name: str,
    settings: Settings = Depends(get_settings),
) -> dict:
    pool = request.app.state.db_pool
    result = _k8s_result(
        await infra_service.k8s_restart_deployment(
            pool, infra_id, settings.secret_key, namespace, name
        )
    )
    await log_audit(
        pool,
        actor="api:infra_admin",
        action="k8s_deployment_restarted",
        target_type="infra",
        target_id=str(infra_id),
        details={"namespace": namespace, "deployment": name},
    )
    return result


@router.post("/{infra_id}/provision")
async def provision_infra(
    request: Request, infra_id: UUID, settings: Settings = Depends(get_settings)
) -> dict:
    pool = request.app.state.db_pool
    existing = await infra_service.get_infra(pool, infra_id)
    if not existing:
        raise HTTPException(404, "Infra entry not found")
    result = await infra_service.provision_infra(pool, infra_id, settings.secret_key)
    await log_audit(
        pool,
        actor="api:infra_admin",
        action="infra_provisioned",
        target_type="infra",
        target_id=str(infra_id),
        details={"status": result.get("status"), "error": result.get("last_error")},
    )
    return result
