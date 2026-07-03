"""SocialConnector — publish posts to social platforms.

Two transports:

- **Native X** (MVP): tokens live in the `social_accounts` table (Fernet
  stored-secret dicts via aegis.crypto). `post()` loads the account fresh,
  refreshes the token when it is near expiry, and — critically for X, which
  rotates the refresh token on EVERY refresh — persists the rotated tokens
  BEFORE first use, so a crash mid-post never strands the account with a
  dead refresh token.
- **Postiz**: a self-hosted Postiz instance holds the platform OAuth and
  does the actual posting; aegis mirrors its channels into `social_accounts`
  (see `routes/social_auth.py::sync_postiz`) with no tokens of its own —
  just `meta.postiz_integration_id` — and posts through Postiz's public API.
  Accounts with that meta key skip token refresh entirely.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import httpx
import structlog

from aegis.connectors._base import HTTPConnector
from aegis.crypto import decrypt_secret, encrypt_secret

logger = structlog.get_logger()

X_TOKEN_URL = "https://api.x.com/2/oauth2/token"
X_TWEETS_URL = "https://api.x.com/2/tweets"
_REFRESH_MARGIN = timedelta(minutes=5)


def _render_text(payload: dict) -> str:
    """Compose post text from {text, link} — shared by every transport."""
    text = (payload.get("text") or "").strip()
    link = (payload.get("link") or "").strip()
    if link:
        text = f"{text}\n\n{link}" if text else link
    return text


class SocialAuthError(RuntimeError):
    """Token refresh failed — the account needs a manual re-connect."""


class SocialConnector(HTTPConnector):
    """Publish to a connected social account. One public method: post()."""

    connector_name = "social"

    def __init__(self, *, db_pool, settings, timeout: float = 30.0) -> None:
        super().__init__(timeout=timeout, db_pool=db_pool)
        self._settings = settings

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=httpx.Timeout(self._timeout, connect=5.0))

    async def post(self, account_id: int, payload: dict) -> str:
        """Publish payload to the account's platform; returns the platform post ref."""
        account = await self._load_account(account_id)
        # Postiz-mirrored accounts hold no OAuth tokens of their own — Postiz
        # does the posting — so route them BEFORE any token refresh attempt.
        if account["meta"].get("postiz_integration_id"):
            return await self._post_postiz(account, payload)
        access_token = await self._refresh_if_needed(account)
        if account["platform"] == "x":
            return await self._post_x(access_token, payload)
        raise ValueError(f"unsupported platform: {account['platform']}")

    async def _load_account(self, account_id: int) -> dict:
        row = await self._db_pool.fetchrow(
            "SELECT id, platform, label, access_token_enc, refresh_token_enc, expires_at, meta "
            "FROM social_accounts WHERE id = $1",
            account_id,
        )
        if row is None:
            raise ValueError(f"social account {account_id} not found")
        return dict(row)

    async def _refresh_if_needed(self, account: dict) -> str:
        """Return a usable access token, refreshing (and persisting) first if near expiry."""
        secret_key = self._settings.secret_key
        access = decrypt_secret(account["access_token_enc"], secret_key)
        expires_at = account["expires_at"]
        if expires_at is not None and expires_at - _REFRESH_MARGIN > datetime.now(UTC):
            return access

        refresh = decrypt_secret(account["refresh_token_enc"], secret_key)
        if not refresh:
            raise SocialAuthError(
                f"social account {account['platform']}/{account['label']} has no refresh "
                "token and the access token is expired — re-connect it from the admin page"
            )
        # X-specific token endpoint; dispatch per platform when more arrive.
        client = await self._ensure_client()
        auth = (
            (self._settings.x_client_id, self._settings.x_client_secret)
            if self._settings.x_client_secret
            else None
        )
        t0 = time.monotonic()
        resp = await client.post(
            X_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": self._settings.x_client_id,
            },
            auth=auth,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        if resp.status_code != 200:
            await self._record("refresh_x", "error", latency_ms, error=resp.text[:200])
            raise SocialAuthError(
                f"x token refresh failed for {account['label']}: "
                f"{resp.status_code} {resp.text[:200]}"
            )
        tok = resp.json()
        new_access = tok["access_token"]
        # X invalidates the old refresh token on every refresh — persist the
        # rotated pair BEFORE using the access token.
        new_refresh = tok.get("refresh_token") or refresh
        await self._db_pool.execute(
            "UPDATE social_accounts SET access_token_enc = $1, refresh_token_enc = $2, "
            "expires_at = now() + make_interval(secs => $3), updated_at = now() WHERE id = $4",
            encrypt_secret(new_access, secret_key),
            encrypt_secret(new_refresh, secret_key),
            int(tok.get("expires_in") or 7200),
            account["id"],
        )
        await self._record("refresh_x", "ok", latency_ms)
        return new_access

    async def _post_x(self, access_token: str, payload: dict) -> str:
        text = _render_text(payload)
        client = await self._ensure_client()
        t0 = time.monotonic()
        resp = await client.post(
            X_TWEETS_URL,
            json={"text": text},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        if resp.status_code not in (200, 201):
            await self._record("post_x", "error", latency_ms, error=resp.text[:200])
            raise RuntimeError(f"x post failed: {resp.status_code} {resp.text[:200]}")
        await self._record("post_x", "ok", latency_ms)
        return str(resp.json()["data"]["id"])

    async def _post_postiz(self, account: dict, payload: dict) -> str:
        postiz_url = self._settings.postiz_url
        api_key = self._settings.postiz_api_key
        if not postiz_url or not api_key:
            raise RuntimeError(
                f"postiz account {account['platform']}/{account['label']} is synced but "
                "postiz_url/postiz_api_key are not configured — set them on the "
                "Integrations page"
            )
        text = _render_text(payload)
        body = {
            "type": "now",
            "shortLink": False,
            "date": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "tags": [],
            "posts": [
                {
                    "integration": {"id": account["meta"]["postiz_integration_id"]},
                    "value": [{"content": text, "image": []}],
                    "settings": {"__type": account["platform"]},
                }
            ],
        }
        client = await self._ensure_client()
        t0 = time.monotonic()
        resp = await client.post(
            f"{postiz_url.rstrip('/')}/api/public/v1/posts",
            json=body,
            headers={"Authorization": api_key},
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        if resp.status_code not in (200, 201):
            await self._record("post_postiz", "error", latency_ms, error=resp.text[:200])
            raise RuntimeError(f"postiz post failed: {resp.status_code} {resp.text[:200]}")
        await self._record("post_postiz", "ok", latency_ms)
        return str(resp.json()[0]["postId"])
