"""Admin: Google integration status (accounts + token scopes) for the admin UI.

Read-only view so the panel can show which accounts are connected and what each
token can do (gmail / calendar / drive). Re-authorization is the existing
/api/admin/gmail/reauth/<label>/initiate browser flow.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request

from aegis.api.auth import verify_auth
from aegis.api.deps import get_settings
from aegis.config import Settings

router = APIRouter(prefix="/api/admin/integrations", dependencies=[Depends(verify_auth)])


@router.get("/notification-budget")
async def notification_budget(
    request: Request, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """Today's proactive-notification count vs the daily budget (Phase 5)."""
    from aegis.services.notifications import budget_status

    return await budget_status(
        request.app.state.db_pool,
        enabled=settings.notification_budget_enabled,
        daily_budget=settings.notification_daily_budget,
    )


@router.get("/config")
async def list_integrations(
    request: Request, settings: Settings = Depends(get_settings)
) -> list[dict[str, Any]]:
    """Integration tokens + webhook secrets — registry, current state, source."""
    from aegis.services.integrations_config import get_integrations

    return await get_integrations(request.app.state.db_pool, settings)


@router.put("/config")
async def save_integration_config(
    request: Request, body: dict[str, Any], settings: Settings = Depends(get_settings)
) -> list[dict[str, Any]]:
    """Set one integration value (secrets encrypted). Re-applies the overlay so
    core (webhook secrets etc.) picks it up live; connectors on next restart."""
    from fastapi import HTTPException

    from aegis.services.integrations_config import (
        apply_config_overrides,
        get_integrations,
        save_integration,
    )

    pool = request.app.state.db_pool
    key = body.get("key")
    if not key:
        raise HTTPException(status_code=400, detail="key is required")
    try:
        await save_integration(pool, settings, key, body.get("value", ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await apply_config_overrides(settings, pool)  # same singleton as app.state.settings
    return await get_integrations(pool, settings)


@router.get("/google-oauth")
async def get_google_oauth(
    request: Request, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """Status of the Google OAuth app (client_id + whether a secret is set)."""
    from aegis.services.google_oauth import google_client_status

    return await google_client_status(request.app.state.db_pool, settings)


@router.put("/google-oauth")
async def put_google_oauth(
    request: Request, body: dict[str, Any], settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """Set the BYO Google OAuth client. client_secret omitted → keep existing."""
    from aegis.services.google_oauth import google_client_status, save_google_client

    client_id = (body.get("client_id") or "").strip()
    if not client_id:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail="client_id is required")
    secret = body.get("client_secret") if "client_secret" in body else None
    await save_google_client(
        request.app.state.db_pool, settings, client_id=client_id, client_secret=secret
    )
    return await google_client_status(request.app.state.db_pool, settings)


@router.get("/google")
async def google_accounts(settings: Settings = Depends(get_settings)) -> list[dict[str, Any]]:
    """Configured Google accounts + each token's scope status.

    Labels come from the AEGIS_GMAIL_ACCOUNTS env var AND from any {label}.json
    token file already present in the token dir — so an account connected from
    the admin UI (which writes a fresh token file) shows up immediately without
    an env edit + restart.
    """
    out: list[dict[str, Any]] = []
    token_dir = Path(settings.gmail_token_dir)
    # label -> email (env accounts carry an email; disk-discovered ones don't)
    labels: dict[str, str] = {}
    for entry in (a for a in (settings.gmail_accounts or "").split(",") if a.strip()):
        label, _, email = entry.partition(":")
        if label.strip():
            labels.setdefault(label.strip(), email.strip())
    if token_dir.exists():
        for tp in sorted(token_dir.glob("*.json")):
            labels.setdefault(tp.stem, "")
    for label, email in labels.items():
        tp = token_dir / f"{label}.json"
        info: dict[str, Any] = {
            "label": label,
            "email": email.strip(),
            "has_token": tp.exists(),
            "has_gmail": False,
            "has_calendar": False,
            "has_drive": False,
        }
        if tp.exists():
            try:
                scopes = json.loads(tp.read_text()).get("scopes", [])
                info["has_gmail"] = any("gmail" in s for s in scopes)
                info["has_calendar"] = any("calendar" in s for s in scopes)
                info["has_drive"] = any("drive" in s for s in scopes)
            except Exception:  # noqa: BLE001 — a malformed token just shows no scopes
                pass
        out.append(info)
    return out
