"""The Postiz channel mirror — one implementation of "make `social_accounts`
match what Postiz actually has connected".

Postiz holds the platform OAuth and does the posting; AEGIS keeps a token-free
mirror row per channel (`meta.postiz_integration_id`) because
`SocialPublishFlow` resolves a Todoist platform label against `social_accounts`,
never against Postiz. A channel connected in Postiz is therefore invisible to
the publish pipeline until that mirror is refreshed.

Two callers share this module so they can never drift (#182):

* ``routes/social_auth.py::sync_postiz`` — the admin "Sync Postiz channels"
  button, which used to be the *only* way the mirror was ever refreshed;
* ``SocialActivities.sync_postiz_channels`` — the throttled sweep
  ``SocialPublishFlow`` runs before every publish tick.

Only channels Postiz reports as enabled are mirrored. Removal is deliberately
NOT handled here: a `social_accounts` row is referenced by `social_outbox`
history, so a channel disconnected in Postiz keeps its row (and its posts) and
simply stops being re-confirmed — `updated_at` going stale is the signal.
"""

from __future__ import annotations

import re

import httpx
import structlog

logger = structlog.get_logger()

#: Postiz public API — the connected-channel list.
INTEGRATIONS_PATH = "/api/public/v1/integrations"


class PostizSyncError(RuntimeError):
    """Postiz answered the integrations call with a non-200."""

    def __init__(self, status: int, body: str = "") -> None:
        super().__init__(f"postiz_sync_failed:{status}")
        self.status = status
        self.body = body


def slugify_label(name: str) -> str:
    """Postiz display name → the `social_accounts.label` slug."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", (name or "").strip().lower())
    return slug.strip("-")


async def fetch_postiz_integrations(
    base_url: str, api_key: str, timeout: float = 30.0
) -> list[dict]:
    """Every channel Postiz knows about (enabled or not). Raises PostizSyncError."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(
            f"{base_url.rstrip('/')}{INTEGRATIONS_PATH}",
            headers={"Authorization": api_key},
        )
    if resp.status_code != 200:
        raise PostizSyncError(resp.status_code, resp.text[:200])
    body = resp.json()
    return list(body or [])


async def mirror_integrations(db_pool, integrations: list[dict]) -> dict:
    """Upsert the enabled channels into `social_accounts`; count the rest.

    Idempotent: the (platform, label) unique key means a re-run refreshes
    `meta`/`updated_at` in place rather than duplicating rows. `updated_at` is
    bumped on every pass even when nothing changed — that is what the scheduled
    sweep uses as its "last successful sync" watermark.
    """
    synced = 0
    skipped_disabled = 0
    for item in integrations:
        if item.get("disabled"):
            skipped_disabled += 1
            continue
        platform = item.get("identifier") or ""
        label = slugify_label(item.get("name") or "") or str(item.get("id"))
        meta = {
            "postiz_integration_id": item.get("id"),
            "via": "postiz",
            "profile": item.get("profile") or "",
            "picture": item.get("picture") or "",
        }
        await db_pool.execute(
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
    return {"synced": synced, "skipped_disabled": skipped_disabled}


async def sync_postiz_channels(
    db_pool, base_url: str, api_key: str, timeout: float = 30.0
) -> dict:
    """Fetch + mirror in one call. Raises PostizSyncError when Postiz refuses."""
    integrations = await fetch_postiz_integrations(base_url, api_key, timeout=timeout)
    result = await mirror_integrations(db_pool, integrations)
    logger.info("postiz_sync_completed", **result)
    return result
