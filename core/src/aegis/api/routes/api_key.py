"""Admin API-key management.

GET  /api/admin/api-key           — status only ({configured, source}), never the key
POST /api/admin/api-key/generate  — mint a new key server-side; the response is
                                    the ONLY time the cleartext is ever exposed

The key is stored encrypted in the ``settings`` table (``services/api_key.py``)
and accepted by ``verify_auth`` as an ``X-API-Key`` alternative to the env
``AEGIS_API_KEY``. Generating requires an already-authenticated admin session
(basic auth / existing key), same as every other admin route.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from aegis.api.auth import verify_auth
from aegis.api.deps import get_settings
from aegis.config import Settings
from aegis.observability import log_audit
from aegis.services.api_key import api_key_status, generate_api_key

router = APIRouter(prefix="/api/admin", dependencies=[Depends(verify_auth)])


@router.get("/api-key")
async def get_api_key_status(
    request: Request, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """API-key status for the admin UI — booleans only, never the key."""
    return await api_key_status(request.app.state.db_pool, settings)


@router.post("/api-key/generate")
async def post_generate_api_key(
    request: Request, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """Generate + store a new API key. The cleartext is returned ONCE here
    and never retrievable again — the UI must tell the user to copy it now."""
    pool = request.app.state.db_pool
    new_key = await generate_api_key(pool, settings)
    await log_audit(
        pool,
        actor="api:api_key",
        action="api_key_generated",
        target_type="setting",
        target_id="api_key",
        details={},  # never the key itself
    )
    return {"api_key": new_key, "configured": True}
