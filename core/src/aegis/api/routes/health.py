"""Health check endpoint — unauthenticated."""

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/health")
async def health(request: Request):
    """Health check. Returns DB status if pool is available."""
    result = {"status": "ok", "version": "0.1.0"}

    if hasattr(request.app.state, "db_pool") and request.app.state.db_pool:
        from aegis.db import check_health

        db = await check_health(request.app.state.db_pool)
        result["postgres"] = db
        if db["status"] != "ok":
            result["status"] = "degraded"

    return result
