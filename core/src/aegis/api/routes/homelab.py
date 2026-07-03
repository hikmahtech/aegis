"""Admin endpoints for Homelab Guardian state + manual runs."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from temporalio.client import Client as TemporalClient

from aegis.api.auth import verify_auth
from aegis.api.deps import get_settings
from aegis.api.routes._flow_trigger import require_temporal_client, start_named_workflow
from aegis.config import Settings

router = APIRouter(
    prefix="/api/admin/homelab",
    tags=["homelab"],
    dependencies=[Depends(verify_auth)],
)


_FLOW_NAMES = {
    "service_drift": "ServiceDriftFlow",
    "cert_radar": "CertRadarFlow",
}


def _default_cfg(flow: str, settings: Settings) -> dict:
    """Build flow-specific defaults from settings for manual triggers."""
    if flow == "cert_radar":
        domains = list(getattr(settings, "homelab_public_domains", None) or [])
        return {"domains": domains} if domains else {}
    return {}


async def _start_workflow(flow: str, cfg: dict, temporal_client: TemporalClient):
    # Both Config dataclasses have full defaults — passing {} is safe.
    return await start_named_workflow(flow, cfg, temporal_client, _FLOW_NAMES)


@router.get("/state")
async def homelab_state(request: Request) -> dict:
    """Return latest rows from the homelab monitoring tables."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        drift = await conn.fetch(
            "SELECT id, service_name, stack_name, drift_type, severity, "
            "detected_at, resolved_at, actual "
            "FROM pandoras_actor.homelab_drift ORDER BY detected_at DESC LIMIT 50"
        )
        certs = await conn.fetch(
            "SELECT DISTINCT ON (domain) domain, cert_serial, not_after, "
            "days_until_expiry, last_alert_threshold, checked_at "
            "FROM pandoras_actor.cert_expiry ORDER BY domain, checked_at DESC"
        )
    return {
        "drift": [dict(r) for r in drift],
        "certs": [dict(r) for r in certs],
    }


@router.post("/{flow}/run")
async def trigger_flow(
    flow: str,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict:
    """Manually trigger a homelab guardian flow by name.

    An optional JSON body overrides flow-specific defaults (e.g. cert_radar
    {"domains": [...]}). When omitted, defaults are pulled from settings —
    cert_radar falls back to settings.homelab_public_domains so manual
    triggers match scheduled behavior.
    """
    client = require_temporal_client(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    cfg = _default_cfg(flow, settings)
    cfg.update(body or {})
    handle = await _start_workflow(flow, cfg, client)
    return {"ok": True, "workflow_id": handle.id}
