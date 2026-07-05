"""Miniflux → channels(kind='rss') sync on core startup."""

from __future__ import annotations

from typing import Any

import httpx
import structlog

logger = structlog.get_logger()


async def seed_rss_from_miniflux(pool: Any, miniflux_url: str, miniflux_api_key: str) -> int:
    """Fetch feeds from Miniflux, upsert into channels.

    Returns number of rows upserted. Safe to call on every startup — idempotent.
    If miniflux_url is empty or Miniflux is unreachable, logs and returns 0.
    """
    if not miniflux_url:
        logger.info("miniflux_url_not_configured", detail="skipping RSS seed from Miniflux")
        return 0
    if not pool:
        return 0

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{miniflux_url.rstrip('/')}/v1/feeds",
                headers={"X-Auth-Token": miniflux_api_key} if miniflux_api_key else {},
            )
            resp.raise_for_status()
            feeds = resp.json()
    except Exception as exc:
        logger.warning("miniflux_fetch_failed", error=str(exc)[:300])
        return 0

    # RSS ingest is owned by whichever agent holds the `research` behavior tag
    # (issue #36), not a literal id. Falls back to "raphael" (the seed research
    # agent) only if no agent currently holds the tag.
    from aegis.services.agents import resolve_tag

    research_agent = await resolve_tag(pool, "research") or "raphael"

    upserted = 0
    async with pool.acquire() as conn:
        for f in feeds:
            feed_url = f.get("feed_url", "")
            if not feed_url:
                continue
            config = {
                "label": f.get("title", ""),
                "agent_id": research_agent,
                "miniflux_id": f.get("id"),
                "last_cursor": None,
            }
            # Pass config as a Python dict — the asyncpg jsonb codec in
            # db/pool.py encodes it once. Calling json.dumps here would
            # double-encode to a scalar string, and `channels.config ||
            # EXCLUDED.config` between an object and a scalar promotes to
            # a jsonb array, corrupting the row.
            #
            # ON CONFLICT preserves channels.config->'last_cursor' so the
            # ingest flows' cursor state isn't wiped on every startup.
            await conn.execute(
                """
                INSERT INTO channels (kind, identifier, config, active)
                VALUES ('rss', $1, $2, true)
                ON CONFLICT (kind, identifier) DO UPDATE
                  SET active = true,
                      config = EXCLUDED.config ||
                               jsonb_build_object(
                                 'last_cursor',
                                 channels.config->'last_cursor'
                               )
                """,
                feed_url,
                config,
            )
            upserted += 1

    logger.info("miniflux_rss_seeded", count=upserted)
    return upserted
