"""BYO Todoist config — API key (encrypted) + managed GTD project IDs, stored in
the settings table and edited from the admin UI.

The API key is read DB-first with the env (``settings.todoist_api_key``) as the
fallback, so existing deployments keep working until the key is set in the UI.
The managed project ids (inbox/next/someday) already live in the settings table
(``todoist_managed_project_ids``); this just adds an edit surface.
"""

from __future__ import annotations

from typing import Any

import structlog

from aegis.crypto import decrypt_secret, encrypt_secret

logger = structlog.get_logger()

KEY_APIKEY = "todoist_api_key"
KEY_PROJECTS = "todoist_managed_project_ids"


async def resolve_todoist_api_key(pool: Any, settings: Any) -> str:
    """The Todoist API key: DB-first (encrypted) with the env value as fallback."""
    try:
        row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1", KEY_APIKEY)
        if row and row["value"]:
            key = decrypt_secret(row["value"].get("api_key_enc"), settings.secret_key)
            if key:
                return key
    except Exception as exc:  # noqa: BLE001 — fall back to env on any read error
        logger.warning("todoist_api_key_read_failed", error=str(exc)[:200])
    return getattr(settings, "todoist_api_key", "") or ""


async def todoist_connector(pool: Any, settings: Any):
    """Build a TodoistConnector from the resolved key, or None when unconfigured."""
    key = await resolve_todoist_api_key(pool, settings)
    if not key:
        return None
    from aegis.connectors.todoist import TodoistConnector

    return TodoistConnector(api_key=key, db_pool=pool, timeout=10.0)


async def get_managed_projects(pool: Any) -> dict:
    row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1", KEY_PROJECTS)
    return (row["value"] if row and row["value"] else {}) or {}


async def save_todoist_config(
    pool: Any, settings: Any, *, api_key: str | None = None, projects: dict | None = None
) -> None:
    """Upsert the key (omitted → keep existing) and/or the managed project ids."""
    if api_key is not None:
        await pool.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()",
            KEY_APIKEY,
            {"api_key_enc": encrypt_secret(api_key, settings.secret_key)},
        )
    if projects is not None:
        clean = {str(k): str(v).strip() for k, v in projects.items() if str(v).strip()}
        await pool.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()",
            KEY_PROJECTS,
            clean,
        )


async def todoist_config_status(pool: Any, settings: Any) -> dict:
    """For the admin UI: is the key set + from where, and the managed project ids."""
    key = await resolve_todoist_api_key(pool, settings)
    row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1", KEY_APIKEY)
    if row and row["value"]:
        source = "db"
    elif getattr(settings, "todoist_api_key", ""):
        source = "env"
    else:
        source = "none"
    return {
        "api_key_set": bool(key),
        "source": source,
        "projects": await get_managed_projects(pool),
    }
