"""Settings key-value store endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from aegis.api.auth import verify_auth
from aegis.observability import log_audit

router = APIRouter(prefix="/api/settings", dependencies=[Depends(verify_auth)])

# Secret-bearing settings rows are managed by their own dedicated pages/endpoints
# and MUST NOT be exposed through this generic key/value editor — their values
# hold API keys / OAuth secrets (stored plaintext when AEGIS_SECRET_KEY is unset).
# Hidden here so they never leave the server via the generic endpoint.
_HIDDEN_KEYS = {"llm_backend", "google_oauth", "todoist_api_key", "slack", "api_key"}
_HIDDEN_PREFIXES = ("integration:",)


def _is_hidden(key: str) -> bool:
    return key in _HIDDEN_KEYS or key.startswith(_HIDDEN_PREFIXES)


@router.get("")
async def list_settings(request: Request) -> list[dict[str, Any]]:
    """List all settings (key + value), excluding secret-bearing rows."""
    pool = request.app.state.db_pool
    rows = await pool.fetch("SELECT key, value, updated_at FROM settings ORDER BY key")
    return [
        {"key": r["key"], "value": r["value"], "updated_at": r["updated_at"]}
        for r in rows
        if not _is_hidden(r["key"])
    ]


@router.get("/{key}")
async def get_setting(key: str, request: Request) -> dict[str, Any]:
    """Get a setting by key. Secret-bearing keys are never returned here."""
    if _is_hidden(key):
        raise HTTPException(status_code=403, detail="Managed on its own page; not readable here.")
    pool = request.app.state.db_pool
    row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1", key)
    if not row:
        return {"key": key, "value": None}
    value = row["value"]
    return {"key": key, "value": value}


@router.put("/{key}")
async def put_setting(key: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Set a setting (upsert). Body: {"value": <any JSON value>}"""
    if _is_hidden(key):
        raise HTTPException(status_code=403, detail="Managed on its own page; edit it there.")
    pool = request.app.state.db_pool
    value = body.get("value", body)
    await pool.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()",
        key,
        value,
    )
    await log_audit(
        pool,
        actor="api:settings",
        action="setting_updated",
        target_type="setting",
        target_id=key,
        details={"key": key, "new_value": str(body.get("value", ""))[:200]},
    )
    return {"key": key, "value": value}
