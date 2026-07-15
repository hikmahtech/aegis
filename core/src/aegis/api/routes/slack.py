"""Slack config — admin (masked) + internal (cleartext, server-to-server).

Admin: GET/PUT under ``/api/admin/slack-config``. Never returns secret values,
only ``*_set`` booleans — mirrors ``routes/llm_backend.py`` / ``routes/todoist.py``.

Internal: GET under ``/api/internal/slack-config``. Returns the resolved
cleartext tokens — comms needs the raw bot/app tokens for
Socket Mode. This is the ONLY endpoint in AEGIS that returns decrypted Slack
secrets; it's gated by ``verify_auth`` (X-API-Key), same as every other route,
so it must only ever be called server-to-server (comms -> core), never from
the browser/admin panel.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from aegis.api.auth import verify_auth
from aegis.api.deps import get_settings
from aegis.config import Settings
from aegis.observability import log_audit
from aegis.services.slack_config import (
    resolve_slack_config,
    save_slack_config,
    slack_config_status,
)

router = APIRouter(prefix="/api/admin", dependencies=[Depends(verify_auth)])
internal_router = APIRouter(prefix="/api/internal", dependencies=[Depends(verify_auth)])


@router.get("/slack-config")
async def get_slack_config(
    request: Request, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """Slack config status for the admin UI — booleans only, never secrets."""
    return await slack_config_status(request.app.state.db_pool, settings)


@router.put("/slack-config")
async def put_slack_config(
    request: Request, body: dict[str, Any], settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """Set the Slack tokens/channel (omitted or blank -> keep existing)."""
    pool = request.app.state.db_pool
    result = await save_slack_config(
        pool,
        settings,
        bot_token=body.get("bot_token"),
        app_token=body.get("app_token"),
        channel=body.get("channel"),
    )
    await log_audit(
        pool,
        actor="api:slack",
        action="slack_config_updated",
        target_type="setting",
        target_id="slack",
    )
    return result


@internal_router.get("/slack-config")
async def get_internal_slack_config(
    request: Request, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """Server-to-server only; the sole endpoint that returns decrypted secrets —
    comms needs the raw tokens for Socket Mode; gated by verify_auth (X-API-Key).
    """
    return await resolve_slack_config(request.app.state.db_pool, settings)
