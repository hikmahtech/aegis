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

logger = structlog.get_logger()

SETTINGS_KEY = "google_oauth"
_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
_TOKEN_URI = "https://oauth2.googleapis.com/token"


async def get_google_client_config(pool: Any, settings: Any) -> dict | None:
    """A ``Flow.from_client_config`` dict, DB-first then the credentials file.
    Returns None when no client is configured."""
    try:
        row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1", SETTINGS_KEY)
        if row and row["value"]:
            v = row["value"]
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
        logger.warning("google_oauth_read_failed", error=str(exc)[:200])

    path = getattr(settings, "gmail_credentials_file", "")
    if path and Path(path).exists():
        try:
            return json.loads(Path(path).read_text())
        except Exception as exc:  # noqa: BLE001
            logger.warning("google_credentials_file_unreadable", error=str(exc)[:200])
    return None


async def save_google_client(
    pool: Any, settings: Any, *, client_id: str, client_secret: str | None = None
) -> None:
    """Upsert the OAuth client. ``client_secret=None`` keeps the existing secret."""
    row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1", SETTINGS_KEY)
    existing = (row["value"] if row and row["value"] else {}) or {}
    secret_enc = existing.get("client_secret_enc")
    if client_secret is not None:
        secret_enc = encrypt_secret(client_secret, settings.secret_key)
    value = {
        "client_id": client_id,
        "client_secret_enc": secret_enc or {"value": "", "encrypted": False},
    }
    await pool.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()",
        SETTINGS_KEY,
        value,
    )


async def google_client_status(pool: Any, settings: Any) -> dict:
    """For the admin UI: is a client configured, its client_id, and the source."""
    row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1", SETTINGS_KEY)
    if row and row["value"] and row["value"].get("client_id"):
        return {"configured": True, "client_id": row["value"]["client_id"], "source": "db"}
    cfg = await get_google_client_config(pool, settings)
    if cfg:
        node = cfg.get("web") or cfg.get("installed") or {}
        return {"configured": True, "client_id": node.get("client_id", ""), "source": "file"}
    return {"configured": False, "client_id": "", "source": "none"}
