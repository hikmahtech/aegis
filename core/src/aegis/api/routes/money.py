"""Admin endpoints for Money Hygiene state + manual runs."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from temporalio.client import Client as TemporalClient

from aegis.api.auth import verify_auth
from aegis.api.deps import get_settings
from aegis.api.routes._flow_trigger import require_temporal_client, start_named_workflow
from aegis.config import Settings

router = APIRouter(
    prefix="/api/admin/money",
    tags=["money"],
    dependencies=[Depends(verify_auth)],
)


_FLOW_NAMES = {
    "money_hygiene": "MoneyHygieneDailyFlow",
    "subscription_audit": "SubscriptionAuditFlow",
}


async def _start_workflow(flow: str, cfg: dict, temporal_client: TemporalClient):
    return await start_named_workflow(flow, cfg, temporal_client, _FLOW_NAMES)


@router.get("/state")
async def money_state(request: Request, settings: Settings = Depends(get_settings)) -> dict:
    """Return active recurring charges + most recent renewal alerts."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        charges = await conn.fetch(
            "SELECT id, account, vendor_name, category, amount_cents, currency, "
            "       monthly_home_equivalent, cadence, next_due_at, status, "
            "       last_seen_at, first_seen_at "
            "FROM maou.recurring_charge "
            "ORDER BY status, next_due_at NULLS LAST"
        )
        upcoming = await conn.fetch(
            "SELECT a.charge_id, a.threshold_days, a.fired_at, "
            "       c.vendor_name, c.amount_cents, c.currency, c.next_due_at "
            "FROM maou.renewal_alert a "
            "JOIN maou.recurring_charge c ON c.id = a.charge_id "
            "ORDER BY a.fired_at DESC LIMIT 50"
        )
    return {
        "charges": [dict(r) for r in charges],
        "upcoming_alerts": [dict(r) for r in upcoming],
        "home_currency": settings.home_currency,
    }


@router.get("/digest")
async def money_digest(request: Request) -> dict:
    """Return the latest monthly subscription digest, or {digest: None}."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT period_start, period_end, summary, sent_at "
            "FROM maou.subscription_digest "
            "ORDER BY period_end DESC LIMIT 1"
        )
    if row is None:
        return {"digest": None}
    return {"digest": dict(row)}


@router.post("/{flow}/run")
async def trigger_flow(
    flow: str,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict:
    """Manually trigger a money hygiene flow by name.

    Returns 503 when no Temporal client is connected, 409 when the
    feature flag is off, 400 for unknown flow names. Body is forwarded
    as the workflow config dict.
    """
    client = require_temporal_client(request)
    if not getattr(settings, "money_hygiene_enabled", False):
        raise HTTPException(
            status_code=409,
            detail="money_hygiene disabled — set AEGIS_MONEY_HYGIENE_ENABLED=true",
        )
    try:
        body = await request.json()
    except Exception:
        body = {}
    handle = await _start_workflow(flow, body or {}, client)
    return {"ok": True, "workflow_id": handle.id}
