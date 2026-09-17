"""BYO Google OAuth app (client_id / client_secret) — OSS config.

A forker registers their OWN Google Cloud OAuth client (the maintainer's won't
authorize other users and must not be committed). The client is stored encrypted
in the ``settings`` table under ``google_oauth`` and edited from the admin UI; the
reauth flow reads it DB-first, falling back to the gitignored credentials file.
Per-account refresh tokens embed the client, so only the initial OAuth (reauth)
needs this — the worker is unaffected.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog

from aegis.crypto import decrypt_secret, encrypt_secret
from aegis.errors import error_text
from aegis.services.settings_store import get_setting, put_setting

logger = structlog.get_logger()

SETTINGS_KEY = "google_oauth"
_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
_TOKEN_URI = "https://oauth2.googleapis.com/token"


async def get_google_client_config(pool: Any, settings: Any) -> dict | None:
    """A ``Flow.from_client_config`` dict, DB-first then the credentials file.
    Returns None when no client is configured."""
    try:
        v = await get_setting(pool, SETTINGS_KEY)
        if v:
            client_id = v.get("client_id")
            secret = decrypt_secret(v.get("client_secret_enc"), settings.secret_key)
            if client_id and secret:
                return {
                    "web": {
                        "client_id": client_id,
                        "client_secret": secret,
                        "auth_uri": _AUTH_URI,
                        "token_uri": _TOKEN_URI,
                    }
                }
    except Exception as exc:  # noqa: BLE001 — fall back to the file on any read error
        logger.warning("google_oauth_read_failed", error=error_text(exc))

    path = getattr(settings, "gmail_credentials_file", "")
    if path and Path(path).exists():
        try:
            return json.loads(Path(path).read_text())
        except Exception as exc:  # noqa: BLE001
            logger.warning("google_credentials_file_unreadable", error=error_text(exc))
    return None


async def save_google_client(
    pool: Any, settings: Any, *, client_id: str, client_secret: str | None = None
) -> None:
    """Upsert the OAuth client. ``client_secret=None`` keeps the existing secret."""
    existing = (await get_setting(pool, SETTINGS_KEY)) or {}
    secret_enc = existing.get("client_secret_enc")
    if client_secret is not None:
        secret_enc = encrypt_secret(client_secret, settings.secret_key)
    value = {
        "client_id": client_id,
        "client_secret_enc": secret_enc or {"value": "", "encrypted": False},
    }
    await put_setting(pool, SETTINGS_KEY, value)


async def google_client_status(pool: Any, settings: Any) -> dict:
    """For the admin UI: is a client configured, its client_id, and the source."""
    stored = await get_setting(pool, SETTINGS_KEY)
    if stored and stored.get("client_id"):
        return {"configured": True, "client_id": stored["client_id"], "source": "db"}
    cfg = await get_google_client_config(pool, settings)
    if cfg:
        node = cfg.get("web") or cfg.get("installed") or {}
        return {"configured": True, "client_id": node.get("client_id", ""), "source": "file"}
    return {"configured": False, "client_id": "", "source": "none"}
