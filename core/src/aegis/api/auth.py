"""Authentication for AEGIS v2 API.

Single-user system: API key via X-API-Key header or Basic auth.
"""

from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from aegis.api.deps import get_settings
from aegis.config import Settings

security = HTTPBasic(auto_error=False)


async def verify_auth(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(security),
    settings: Settings = Depends(get_settings),
) -> bool:
    """Verify authentication via API key or Basic auth."""
    api_key = request.headers.get("X-API-Key")
    if api_key and settings.api_key and secrets.compare_digest(api_key, settings.api_key):
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
