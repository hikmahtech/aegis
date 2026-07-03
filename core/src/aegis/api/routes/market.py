"""Market summary endpoint — web index quotes for briefings and the admin UI."""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, Request

from aegis.api.auth import verify_auth

logger = structlog.get_logger()

router = APIRouter(prefix="/api/market", dependencies=[Depends(verify_auth)])


@router.get("/summary")
async def market_summary(request: Request) -> dict[str, Any]:
    """Return quotes for the configured overview indices (FinanceConnector).

    Returns {"available": false} if the connector is missing or every
    configured index errored — the briefing simply drops its market section.
    """
    fin = getattr(request.app.state, "finance_connector", None)
    if not fin:
        return {"available": False}

    try:
        quotes = await fin.get_overview()
    except Exception as exc:
        logger.warning("market_summary_failed", error=str(exc))
        return {"available": False}

    ok = [q for q in quotes if not q.get("error")]
    if not ok:
        return {"available": False}
    return {"available": True, "indices": ok}
