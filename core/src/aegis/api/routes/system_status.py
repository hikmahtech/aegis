"""System status — AEGIS's own health, folding in db/services/temporal probes.

Distinct from routes/health.py (unauthenticated liveness probe used by
orchestrators/load balancers): this is the authenticated admin-facing view
used by the System Monitoring UI, with richer per-probe detail. Every probe
is wrapped so a single failure degrades that section instead of 500ing the
whole endpoint.
"""

from __future__ import annotations

import asyncio
import shlex
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, Depends, Request

from aegis.api.auth import verify_auth
from aegis.api.deps import get_settings
from aegis.config import Settings
from aegis.connectors._ssh import build_ssh_args
from aegis.connectors._subprocess import kill_and_wait
from aegis.services.infra import get_aegis_host, ssh_key_file

logger = structlog.get_logger()

router = APIRouter(prefix="/api/admin/system", dependencies=[Depends(verify_auth)])

_SERVICES_TIMEOUT = 20


async def _probe_db(pool) -> dict[str, Any]:
    try:
        from aegis.db import check_health

        return await check_health(pool)
    except Exception as exc:
        logger.warning("system_status_db_probe_failed", error=str(exc))
        return {"status": "error", "error": str(exc)}


async def _probe_temporal(settings: Settings) -> dict[str, Any]:
    base = (settings.temporal_api_url or "").rstrip("/")
    if not base:
        return {"status": "unknown", "note": "temporal_api_url not configured"}
    url = f"{base}/api/v1/namespaces/default/workflows"
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(url, params={"pageSize": 1})
            resp.raise_for_status()
        return {"status": "ok"}
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:300]}


def _stack_filter_args(stack: str) -> list[str]:
    """`docker service ls` filter args scoping output to one swarm stack.

    Matches the label `docker stack deploy` stamps on every service it creates,
    so the System Monitoring page shows AEGIS's own services rather than every
    stack on the swarm. Empty stack ⇒ no filter (show all).
    """
    if not stack:
        return []
    return ["--filter", f"label=com.docker.stack.namespace={stack}"]


async def _list_docker_services_via_context(docker_context: str, stack: str) -> dict[str, Any]:
    cmd = ["docker"]
    if docker_context:
        cmd += ["--context", docker_context]
    cmd += ["service", "ls", *_stack_filter_args(stack), "--format", "{{.Name}} {{.Replicas}} {{.Image}}"]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=_SERVICES_TIMEOUT)
        except TimeoutError:
            return {"status": "error", "error": "docker service ls timed out", "services": []}
        if proc.returncode != 0:
            return {"status": "error", "error": err.decode()[:300], "services": []}
    finally:
        await kill_and_wait(proc)
    return {"status": "ok", "services": _parse_service_lines(out.decode())}


async def _list_docker_services_via_ssh(
    host: str, user: str, key_file: str, port: int, stack: str
) -> dict[str, Any]:
    stack_filter = (
        f"--filter label=com.docker.stack.namespace={shlex.quote(stack)} " if stack else ""
    )
    remote_cmd = (
        f"docker service ls {stack_filter}--format '{{{{.Name}}}} {{{{.Replicas}}}} {{{{.Image}}}}'"
    )
    args = build_ssh_args(host, user, key_file, remote_cmd, connect_timeout=10)
    if port and port != 22:
        args = args[:-2] + ["-p", str(port)] + args[-2:]
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=_SERVICES_TIMEOUT)
        except TimeoutError:
            return {"status": "error", "error": "ssh docker service ls timed out", "services": []}
        if proc.returncode != 0:
            return {"status": "error", "error": err.decode()[:300], "services": []}
    finally:
        await kill_and_wait(proc)
    return {"status": "ok", "services": _parse_service_lines(out.decode())}


def _parse_service_lines(output: str) -> list[dict]:
    services = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 2)
        name = parts[0] if len(parts) > 0 else ""
        replicas = parts[1] if len(parts) > 1 else ""
        image = parts[2] if len(parts) > 2 else ""
        stack = name.split("_", 1)[0] if "_" in name else ""
        services.append({"name": name, "stack": stack, "replicas": replicas, "image": image})
    return services


async def _probe_services(pool, settings: Settings) -> dict[str, Any]:
    try:
        aegis_host = await get_aegis_host(pool)
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:300], "services": []}

    if not aegis_host:
        return {
            "status": "unconfigured",
            "services": [],
            "note": "configure an infrastructure entry with 'hosts_aegis' to see running services",
        }

    stack = (settings.aegis_stack_name or "").strip()
    try:
        if aegis_host.get("docker_context"):
            result = await _list_docker_services_via_context(aegis_host["docker_context"], stack)
        elif aegis_host.get("host") and aegis_host.get("ssh_user"):
            with ssh_key_file(aegis_host, settings.secret_key) as key_file:
                if not key_file:
                    return {
                        "status": "unconfigured",
                        "services": [],
                        "note": "hosts_aegis entry has no SSH key (stored or ssh_key_ref)",
                    }
                result = await _list_docker_services_via_ssh(
                    aegis_host["host"],
                    aegis_host["ssh_user"],
                    key_file,
                    aegis_host.get("ssh_port") or 22,
                    stack,
                )
        else:
            return {
                "status": "unconfigured",
                "services": [],
                "note": "hosts_aegis entry has neither docker_context nor complete ssh fields",
            }
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:300], "services": []}

    result["infra_slug"] = aegis_host["slug"]
    return result


def _auth_mode(settings: Settings) -> str:
    """How the API authenticates callers — surfaced so an auth-disabled
    deployment is visible in the admin UI rather than only in the boot log (#88).

    Reflects the env-configured credentials only. An admin-generated API key
    lives encrypted in the settings table (services/api_key.py) and also works
    in every mode except "disabled"; it is deliberately not probed here.

    "disabled" means verify_auth accepts anonymous requests on every route.
    """
    if settings.auth_disabled:
        return "disabled"
    has_basic = bool(settings.admin_username and settings.admin_password)
    has_key = bool(settings.api_key)
    if has_basic and has_key:
        return "basic+api_key"
    if has_key:
        return "api_key"
    if has_basic:
        return "basic"
    return "none"


@router.get("/status")
async def system_status(
    request: Request, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    pool = request.app.state.db_pool

    db_result, services_result, temporal_result = await asyncio.gather(
        _probe_db(pool),
        _probe_services(pool, settings),
        _probe_temporal(settings),
        return_exceptions=True,
    )

    def _safe(result: Any, label: str) -> dict[str, Any]:
        if isinstance(result, Exception):
            logger.warning("system_status_probe_failed", probe=label, error=str(result))
            return {"status": "error", "error": str(result)[:300]}
        return result

    db = _safe(db_result, "db")
    services = _safe(services_result, "services")
    temporal = _safe(temporal_result, "temporal")

    overall = "ok"
    if db.get("status") != "ok":
        overall = "degraded"
    if services.get("status") == "error" or temporal.get("status") == "error":
        overall = "degraded"

    return {
        "status": overall,
        "auth_mode": _auth_mode(settings),
        "db": db,
        "services": services,
        "temporal": temporal,
    }
