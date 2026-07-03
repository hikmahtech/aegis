"""Authentication for AEGIS v2 API.

Single-user system: API key via X-API-Key header or Basic auth.
"""

from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from aegis.api.deps import get_settings
from aegis.config import Settings
from aegis.services.api_key import resolve_api_key

security = HTTPBasic(auto_error=False)


async def verify_auth(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(security),
    settings: Settings = Depends(get_settings),
) -> bool:
    """Verify authentication via API key or Basic auth.

    The ``X-API-Key`` header is checked against BOTH the env ``AEGIS_API_KEY``
    (legacy fallback) and the admin-generated key stored encrypted in the
    ``settings`` table (``services/api_key.py``, short TTL cache — a freshly
    generated key applies within seconds without a restart).

    ``auth_disabled=true`` (AEGIS_AUTH_DISABLED) bypasses both checks — for
    deployments fronted by an authenticating proxy (e.g. Cloudflare Access).
    Webhook HMAC verification (api/routes/webhooks.py) is separate and
    unaffected by this flag.
    """
    if settings.auth_disabled:
        return True

    api_key = request.headers.get("X-API-Key")
    if api_key:
        if settings.api_key and secrets.compare_digest(api_key, settings.api_key):
            return True
        pool = getattr(request.app.state, "db_pool", None)
        if pool is not None:
            db_key = await resolve_api_key(pool, settings)
            if db_key and secrets.compare_digest(api_key, db_key):
                return True

    if credentials:
        correct_username = secrets.compare_digest(credentials.username, settings.admin_username)
        correct_password = secrets.compare_digest(credentials.password, settings.admin_password)
        if correct_username and correct_password:
            return True

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid credentials",
        headers={"WWW-Authenticate": "Basic"},
    )
