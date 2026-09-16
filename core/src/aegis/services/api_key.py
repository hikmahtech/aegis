"""Admin-generated API key — stored encrypted in the ``settings`` table.

The key is generated server-side (``secrets.token_urlsafe(32)``) from the admin
UI, returned in cleartext exactly once, and stored encrypted with
``AEGIS_SECRET_KEY`` (same crypto as the Slack / integration secrets). The env
var ``AEGIS_API_KEY`` remains a fallback so existing deployments keep working.

``verify_auth`` resolves the DB key through a short TTL cache (mirrors the
``llm_backend`` config cache) so the hot auth path doesn't hit the DB per
request, while a freshly generated key still applies within seconds.

The admin UI only ever sees ``api_key_status`` (booleans) — never the stored
key. The one cleartext exposure is the generate response itself.
"""

from __future__ import annotations

import os
import secrets
import time
from typing import Any

import structlog

from aegis.crypto import decrypt_secret, encrypt_secret
from aegis.errors import error_text
from aegis.services.settings_store import get_setting, put_setting

logger = structlog.get_logger()

SETTINGS_KEY = "api_key"

_CACHE_TTL = 30.0
_cache: dict[str, Any] = {"key": None, "ts": 0.0}


def invalidate_api_key_cache() -> None:
    _cache.update(key=None, ts=0.0)


async def resolve_api_key(pool: Any, settings: Any, *, use_cache: bool = True) -> str:
    """The DB-stored API key, cleartext ("" when unset). Server-side only.

    Never raises — any read/decrypt failure resolves to "" so auth simply
    falls through to the other credential checks.
    """
    now = time.monotonic()
    if use_cache and _cache["key"] is not None and now - _cache["ts"] < _CACHE_TTL:
        return _cache["key"]
    key = ""
    try:
        stored = await get_setting(pool, SETTINGS_KEY)
        if stored:
            key = decrypt_secret(stored.get("key_enc"), settings.secret_key)
    except Exception as exc:  # noqa: BLE001 — auth must not 500 on a settings read
        logger.warning("api_key_read_failed", error=error_text(exc))
        return ""
    _cache.update(key=key, ts=now)
    return key


async def generate_api_key(pool: Any, settings: Any) -> str:
    """Generate + store a new API key; return the cleartext exactly once.

    Overwrites any previously stored key (single-user system — one active
    DB key). The env ``AEGIS_API_KEY`` fallback is unaffected.
    """
    new_key = secrets.token_urlsafe(32)
    value = {"key_enc": encrypt_secret(new_key, settings.secret_key)}
    await put_setting(pool, SETTINGS_KEY, value)
    invalidate_api_key_cache()
    return new_key


async def api_key_status(pool: Any, settings: Any) -> dict[str, Any]:
    """For the admin UI: is a key configured + from where. Never cleartext."""
    db_key = await resolve_api_key(pool, settings, use_cache=False)
    if db_key:
        source = "db"
    elif getattr(settings, "api_key", "") or os.environ.get("AEGIS_API_KEY"):
        source = "env"
    else:
        source = "none"
    return {"configured": source != "none", "source": source}
