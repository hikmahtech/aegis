"""Gmail reauth OAuth initiate + callback.

When a Gmail token refresh fails, GmailActivities raises GmailAuthExpiredError
which the flow translates into an InteractionFlow(kind='ack', timeout_policy='hold').
The Telegram card embeds a link to /api/admin/gmail/reauth/{label}/initiate.
The callback saves the new token at config/gmail_tokens/{label}.json AND
resolves the pending interaction so the flow resumes.
"""

from __future__ import annotations

import secrets
from pathlib import Path

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

from aegis.api.auth import verify_auth
from aegis.api.deps import get_settings
from aegis.config import Settings

logger = structlog.get_logger()

router = APIRouter(
    prefix="/api/admin/gmail/reauth",
    tags=["gmail-reauth"],
    dependencies=[Depends(verify_auth)],
)


_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.readonly",
    # Read-only Drive — used by the knowledge /ingest-drive seeder. Adding this
    # means each account must be re-authorized once for Drive reads to work.
    "https://www.googleapis.com/auth/drive.readonly",
]


def _redirect_uri(settings: Settings, label: str) -> str:
    base = (settings.aegis_ui_url or "").rstrip("/")
    return f"{base}/api/admin/gmail/reauth/{label}/callback"


def _pkce_path(settings: Settings, nonce: str) -> Path:
    # Persist the PKCE code_verifier keyed by a random nonce carried in state,
    # because initiate and callback run as separate requests with fresh Flow
    # instances. google-auth-oauthlib auto-generates a verifier and Google
    # rejects the token exchange without it.
    return Path(settings.gmail_token_dir) / f".pkce_{nonce}"


async def _build_flow(request: Request, settings: Settings, label: str):
    """Build a Google OAuth Flow from the configured client (DB-first, file fallback)."""
    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError as exc:
        logger.error("google_auth_oauthlib_missing", error=str(exc))
        raise HTTPException(status_code=503, detail="oauth_library_not_installed") from exc

    from aegis.services.google_oauth import get_google_client_config

    client_config = await get_google_client_config(request.app.state.db_pool, settings)
    if not client_config:
        raise HTTPException(status_code=503, detail="google_oauth_client_not_configured")
    return Flow.from_client_config(
        client_config, scopes=_SCOPES, redirect_uri=_redirect_uri(settings, label)
    )


@router.get("/{label}/initiate")
async def initiate_reauth(
    label: str,
    request: Request,
    interaction_id: str | None = Query(default=None),
    settings: Settings = Depends(get_settings),
):
    """Start Google OAuth2 flow. Redirects to Google's consent page."""
    flow = await _build_flow(request, settings, label)
    nonce = secrets.token_urlsafe(16)
    state_payload = f"{label}:{interaction_id or ''}:{nonce}"
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        state=state_payload,
    )
    if flow.code_verifier:
        pkce_path = _pkce_path(settings, nonce)
        pkce_path.parent.mkdir(parents=True, exist_ok=True)
        pkce_path.write_text(flow.code_verifier)
    logger.info("gmail_reauth_initiated", label=label, has_interaction=bool(interaction_id))
    return RedirectResponse(auth_url, status_code=302)


@router.get("/{label}/callback")
async def callback_reauth(
    label: str,
    request: Request,
    code: str = Query(...),
    state: str = Query(default=""),
    settings: Settings = Depends(get_settings),
):
    """Exchange code for tokens, save to disk, resolve the pending interaction."""
    flow = await _build_flow(request, settings, label)

    # Restore PKCE code_verifier written during /initiate keyed by state nonce.
    parts = state.split(":", 2)
    interaction_id = parts[1] if len(parts) >= 2 and parts[1] else None
    nonce = parts[2] if len(parts) >= 3 and parts[2] else None
    pkce_path = _pkce_path(settings, nonce) if nonce else None
    if pkce_path and pkce_path.exists():
        flow.code_verifier = pkce_path.read_text().strip()
        try:
            pkce_path.unlink()
        except OSError:
            pass

    flow.fetch_token(code=code)
    creds = flow.credentials

    # Save to the same path GmailActivities reads.
    token_dir = Path(settings.gmail_token_dir)
    token_dir.mkdir(parents=True, exist_ok=True)
    token_path = token_dir / f"{label}.json"
    token_path.write_text(creds.to_json())
    logger.info("gmail_reauth_token_saved", label=label, path=str(token_path))

    if interaction_id:
        # Resolve via HTTP POST to ourself — uses the same choke-point as Telegram
        # callbacks, which signals the waiting workflow. Keeps one resolve path.
        base_url = str(request.base_url).rstrip("/")
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.post(
                    f"{base_url}/api/interactions/{interaction_id}/resolve",
                    json={"response": {"value": "ack", "reauthed_label": label}},
                    headers={"X-API-Key": settings.api_key} if settings.api_key else {},
                )
                logger.info(
                    "gmail_reauth_interaction_resolved",
                    interaction_id=interaction_id,
                    status=resp.status_code,
                )
            except Exception as exc:
                logger.warning(
                    "gmail_reauth_resolve_failed",
                    interaction_id=interaction_id,
                    error=str(exc)[:200],
                )

    return {"ok": True, "label": label, "interaction_resolved": interaction_id}
