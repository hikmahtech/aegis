"""Social account connect/callback OAuth routes (MVP: X/Twitter, PKCE).

Modeled on gmail_reauth.py, but unlike Gmail's legacy token files the tokens
land in the `social_accounts` table as Fernet stored-secret dicts
(aegis.crypto). One-time per (platform, label) account; re-connecting
overwrites the row.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import time
from urllib.parse import urlencode

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

from aegis.api.auth import verify_auth
from aegis.api.deps import get_settings
from aegis.config import Settings
from aegis.crypto import encrypt_secret

logger = structlog.get_logger()

router = APIRouter(
    prefix="/api/admin/social",
    tags=["social-auth"],
    dependencies=[Depends(verify_auth)],
)

_X_AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
_X_TOKEN_URL = "https://api.x.com/2/oauth2/token"
_X_SCOPES = "tweet.read tweet.write users.read offline.access"

# PKCE code_verifier stash keyed by the state nonce — initiate and callback
# are separate requests. ponytail: in-process dict, fine for the single-process
# core; stash in the settings table if core ever runs multiple replicas.
_PKCE_STATES: dict[str, dict] = {}
_PKCE_TTL_SECONDS = 600


def _prune_pkce_states() -> None:
    cutoff = time.monotonic() - _PKCE_TTL_SECONDS
    for k in [k for k, v in _PKCE_STATES.items() if v["created"] < cutoff]:
        _PKCE_STATES.pop(k, None)


def _require_x(platform: str) -> None:
    if platform != "x":
        raise HTTPException(status_code=404, detail=f"unsupported_platform:{platform}")


def _redirect_uri(settings: Settings) -> str:
    base = (settings.aegis_ui_url or "").rstrip("/")
    return f"{base}/api/admin/social/x/callback"


@router.get("/{platform}/connect")
async def connect_account(
    platform: str,
    label: str = Query(default="default"),
    settings: Settings = Depends(get_settings),
):
    """Start the platform's OAuth flow. Redirects to the consent page."""
    _require_x(platform)
    if not settings.x_client_id:
        raise HTTPException(status_code=503, detail="x_oauth_client_not_configured")

    _prune_pkce_states()
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    state = secrets.token_urlsafe(16)
    _PKCE_STATES[state] = {"verifier": verifier, "label": label, "created": time.monotonic()}

    auth_url = _X_AUTHORIZE_URL + "?" + urlencode(
        {
            "response_type": "code",
            "client_id": settings.x_client_id,
            "redirect_uri": _redirect_uri(settings),
            "scope": _X_SCOPES,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    logger.info("social_connect_initiated", platform=platform, label=label)
    return RedirectResponse(auth_url, status_code=302)


@router.get("/{platform}/callback")
async def connect_callback(
    platform: str,
    request: Request,
    code: str = Query(...),
    state: str = Query(default=""),
    settings: Settings = Depends(get_settings),
):
    """Exchange the code for tokens and upsert the social_accounts row."""
    _require_x(platform)
    entry = _PKCE_STATES.pop(state, None)
    if entry is None:
        raise HTTPException(status_code=400, detail="unknown_or_expired_state")
    label = entry["label"]

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _redirect_uri(settings),
        "code_verifier": entry["verifier"],
        "client_id": settings.x_client_id,
    }
    # Confidential clients authenticate with Basic auth; public clients rely
    # on client_id in the body.
    auth = (
        (settings.x_client_id, settings.x_client_secret) if settings.x_client_secret else None
    )
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(_X_TOKEN_URL, data=data, auth=auth)
    if resp.status_code != 200:
        logger.warning(
            "social_token_exchange_failed",
            platform=platform,
            status=resp.status_code,
            body=resp.text[:200],
        )
        raise HTTPException(status_code=502, detail=f"token_exchange_failed:{resp.status_code}")
    tok = resp.json()

    expires_in = int(tok.get("expires_in") or 7200)
    await request.app.state.db_pool.execute(
        """
        INSERT INTO social_accounts
          (platform, label, access_token_enc, refresh_token_enc, expires_at, meta, updated_at)
        VALUES ($1, $2, $3, $4, now() + make_interval(secs => $5), $6, now())
        ON CONFLICT (platform, label) DO UPDATE
          SET access_token_enc = EXCLUDED.access_token_enc,
              refresh_token_enc = EXCLUDED.refresh_token_enc,
              expires_at = EXCLUDED.expires_at,
              meta = EXCLUDED.meta,
              updated_at = now()
        """,
        platform,
        label,
        encrypt_secret(tok.get("access_token") or "", settings.secret_key),
        encrypt_secret(tok.get("refresh_token") or "", settings.secret_key),
        expires_in,
        {"scope": tok.get("scope", "")},
    )
    logger.info("social_account_connected", platform=platform, label=label)
    return {"ok": True, "platform": platform, "label": label, "expires_in": expires_in}


@router.get("/accounts")
async def list_accounts(request: Request):
    """Connected accounts for the admin page — never returns token values."""
    rows = await request.app.state.db_pool.fetch(
        "SELECT id, platform, label, expires_at, updated_at, meta "
        "FROM social_accounts ORDER BY platform, label"
    )
    return [
        {
            "id": r["id"],
            "platform": r["platform"],
            "label": r["label"],
            "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            "scope": (r["meta"] or {}).get("scope", ""),
            "via": (r["meta"] or {}).get("via", "native"),
        }
        for r in rows
    ]


def _slugify_label(name: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", (name or "").strip().lower())
    return slug.strip("-")


@router.post("/postiz/sync")
async def sync_postiz(request: Request, settings: Settings = Depends(get_settings)):
    """Mirror the self-hosted Postiz instance's connected channels into
    social_accounts. Postiz holds the platform OAuth and does the actual
    posting — mirrored rows carry no tokens, just `meta.postiz_integration_id`
    so SocialConnector.post() knows to route through Postiz."""
    if not settings.postiz_url or not settings.postiz_api_key:
        raise HTTPException(status_code=503, detail="postiz_not_configured")

    base = settings.postiz_url.rstrip("/")
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{base}/api/public/v1/integrations",
            headers={"Authorization": settings.postiz_api_key},
        )
    if resp.status_code != 200:
        logger.warning("postiz_sync_failed", status=resp.status_code, body=resp.text[:200])
        raise HTTPException(status_code=502, detail=f"postiz_sync_failed:{resp.status_code}")

    integrations = resp.json()
    synced = 0
    skipped_disabled = 0
    for item in integrations:
        if item.get("disabled"):
            skipped_disabled += 1
            continue
        platform = item.get("identifier") or ""
        label = _slugify_label(item.get("name") or "") or str(item.get("id"))
        meta = {
            "postiz_integration_id": item.get("id"),
            "via": "postiz",
            "profile": item.get("profile") or "",
            "picture": item.get("picture") or "",
        }
        await request.app.state.db_pool.execute(
            """
            INSERT INTO social_accounts (platform, label, meta, updated_at)
            VALUES ($1, $2, $3, now())
            ON CONFLICT (platform, label) DO UPDATE
              SET meta = EXCLUDED.meta, updated_at = now()
            """,
            platform,
            label,
            meta,
        )
        synced += 1
    logger.info("postiz_sync_completed", synced=synced, skipped_disabled=skipped_disabled)
    return {"synced": synced, "skipped_disabled": skipped_disabled}
