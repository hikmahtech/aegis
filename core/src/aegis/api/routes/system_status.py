"""System status — AEGIS's own health, folding in the db and temporal probes.

Distinct from routes/health.py (unauthenticated liveness probe used by
orchestrators/load balancers): this is the authenticated admin-facing view
used by the System Monitoring UI, with richer per-probe detail. Every probe
is wrapped so a single failure degrades that section instead of 500ing the
whole endpoint.

The running-services list went with the infra registry (its `hosts_aegis`
entry said where to run `docker service ls`); the registry left v1 with the
development lane, so this view no longer lists swarm services.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, Depends, Request

from aegis.api.auth import verify_auth
from aegis.api.deps import get_settings
from aegis.config import Settings
from aegis.errors import error_text

logger = structlog.get_logger()

router = APIRouter(prefix="/api/admin/system", dependencies=[Depends(verify_auth)])

async def _probe_db(pool) -> dict[str, Any]:
    try:
        from aegis.db import check_health

        return await check_health(pool)
    except Exception as exc:
        logger.warning("system_status_db_probe_failed", error=error_text(exc, 500))
        return {"status": "error", "error": error_text(exc, 500)}


async def _probe_temporal(settings: Settings) -> dict[str, Any]:
    base = (settings.temporal_api_url or "").rstrip("/")
    if not base:
        return {"status": "unknown", "note": "temporal_api_url not configured"}
    url = f"{base}/api/v1/namespaces/{settings.temporal_namespace}/workflows"
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(url, params={"pageSize": 1})
            resp.raise_for_status()
        return {"status": "ok"}
    except Exception as exc:
        return {"status": "error", "error": error_text(exc, 300)}


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

    db_result, temporal_result = await asyncio.gather(
        _probe_db(pool),
        _probe_temporal(settings),
        return_exceptions=True,
    )

    def _safe(result: Any, label: str) -> dict[str, Any]:
        if isinstance(result, Exception):
            logger.warning("system_status_probe_failed", probe=label, error=str(result))
            return {"status": "error", "error": str(result)[:300]}
        return result

    db = _safe(db_result, "db")
    temporal = _safe(temporal_result, "temporal")

    overall = "ok"
    if db.get("status") != "ok":
        overall = "degraded"
    if temporal.get("status") == "error":
        overall = "degraded"

    return {
        "status": overall,
        "auth_mode": _auth_mode(settings),
        "db": db,
        "temporal": temporal,
    }
