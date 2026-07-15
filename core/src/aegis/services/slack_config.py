"""BYO Slack config — bot/app tokens (encrypted) + channel,
stored in the settings table and edited from the admin UI.

Slack has no dedicated fields on ``Settings`` today (unlike the LLM backend or
Todoist), so the env fallback reads ``os.environ`` directly: ``AEGIS_SLACK_BOT_TOKEN``
/ ``AEGIS_SLACK_APP_TOKEN`` / ``AEGIS_CHANNEL``
(these match comms' own ``aegis_comms.config`` aliases). DB-first, so existing
deployments keep working via env until the tokens are set in the UI.

comms talks to Slack over Socket Mode and needs the raw tokens — that's what
``resolve_slack_config`` is for (server-to-server only, via the internal route).
The admin UI only ever sees ``slack_config_status`` (booleans, never secrets).
"""

from __future__ import annotations

import os
from typing import Any

import structlog

from aegis.crypto import decrypt_secret, encrypt_secret

logger = structlog.get_logger()

SETTINGS_KEY = "slack"


async def resolve_slack_config(pool: Any, settings: Any) -> dict[str, Any]:
    """The Slack config, cleartext: DB-first (decrypted) with env as fallback.

    Server-to-server only — this is the sole function that returns decrypted
    secrets. Do not expose its output through an admin-facing endpoint.
    """
    bot_token = ""
    app_token = ""
    channel = ""
    try:
        row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1", SETTINGS_KEY)
        if row and row["value"]:
            v = row["value"]
            bot_token = decrypt_secret(v.get("bot_token_enc"), settings.secret_key)
            app_token = decrypt_secret(v.get("app_token_enc"), settings.secret_key)
            channel = v.get("channel") or ""
    except Exception as exc:  # noqa: BLE001 — fall back to env on any read error
        logger.warning("slack_config_read_failed", error=str(exc)[:200])

    if not bot_token:
        bot_token = os.environ.get("AEGIS_SLACK_BOT_TOKEN", "")
    if not app_token:
        app_token = os.environ.get("AEGIS_SLACK_APP_TOKEN", "")
    if not channel:
        channel = os.environ.get("AEGIS_CHANNEL", "")

    return {
        "configured": bool(bot_token and app_token),
        "bot_token": bot_token,
        "app_token": app_token,
        "channel": channel,
    }


async def save_slack_config(
    pool: Any,
    settings: Any,
    *,
    bot_token: str | None = None,
    app_token: str | None = None,
    channel: str | None = None,
) -> dict[str, Any]:
    """Upsert the Slack config. Write-only fields: ``None`` or ``""`` keeps the
    existing encrypted value (so a blank admin-UI field never wipes a saved
    secret) — mirrors ``save_llm_backend``/``save_todoist_config``.
    """
    row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1", SETTINGS_KEY)
    existing = (row["value"] if row and row["value"] else {}) or {}

    bot_token_enc = existing.get("bot_token_enc")
    if bot_token:
        bot_token_enc = encrypt_secret(bot_token, settings.secret_key)

    app_token_enc = existing.get("app_token_enc")
    if app_token:
        app_token_enc = encrypt_secret(app_token, settings.secret_key)

    new_channel = existing.get("channel")
    if channel:
        new_channel = channel

    value = {
        "bot_token_enc": bot_token_enc or {"value": "", "encrypted": False},
        "app_token_enc": app_token_enc or {"value": "", "encrypted": False},
        "channel": new_channel,
    }
    await pool.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()",
        SETTINGS_KEY,
        value,
    )
    return await slack_config_status(pool, settings)


async def slack_config_status(pool: Any, settings: Any) -> dict[str, Any]:
    """For the admin UI: is each field set + from where. Never cleartext."""
    resolved = await resolve_slack_config(pool, settings)
    row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1", SETTINGS_KEY)
    if row and row["value"] and (row["value"].get("bot_token_enc") or {}).get("value"):
        source = "db"
    elif os.environ.get("AEGIS_SLACK_BOT_TOKEN") or os.environ.get("AEGIS_SLACK_APP_TOKEN"):
        source = "env"
    else:
        source = "none"
    return {
        "bot_token_set": bool(resolved["bot_token"]),
        "app_token_set": bool(resolved["app_token"]),
        "channel": resolved["channel"] or None,
        "configured": resolved["configured"],
        "source": source,
    }
