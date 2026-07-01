"""RaindropActivities — bookmark poll + channel cursor helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog
from temporalio import activity

logger = structlog.get_logger()

_BASE = "https://api.raindrop.io/rest/v1"


@dataclass
class PollBookmarksInput:
    since_cursor: str | None  # ISO timestamp


@dataclass
class PollBookmarksResult:
    bookmarks: list[dict] = field(default_factory=list)
    latest_created: str | None = None


@dataclass
class RaindropActivities:
    raindrop_api_token: str
    db_pool: Any = None
    http_client: httpx.AsyncClient | None = None

    @activity.defn
    async def poll_bookmarks(self, input: PollBookmarksInput) -> PollBookmarksResult:
        if not self.raindrop_api_token:
            logger.warning("raindrop_token_missing")
            return PollBookmarksResult()

        params: dict[str, Any] = {"sort": "-created", "perpage": 50}
        if input.since_cursor:
            # Raindrop's `created:>` search operator is date-granular — full
            # ISO timestamps return 0 rows. Strip to YYYY-MM-DD here; the
            # post-fetch filter below applies the full-ISO precision.
            date_part = input.since_cursor[:10]
            params["search"] = f"created:>{date_part}"

        client = self.http_client or httpx.AsyncClient()
        try:
            resp = await client.get(
                f"{_BASE}/raindrops/0",
                headers={"Authorization": f"Bearer {self.raindrop_api_token}"},
                params=params,
            )
            resp.raise_for_status()
        finally:
            if self.http_client is None:
                await client.aclose()

        data = resp.json()
        items = data.get("items") or []

        bookmarks: list[dict] = []
        latest_created: str | None = None

        for item in items:
            created = item.get("created", "")
            # API filter is date-granular, so same-day already-ingested items
            # come back on every tick. Drop them here via full-ISO compare so
            # the cursor only advances on a genuinely-new bookmark.
            if input.since_cursor and created and created <= input.since_cursor:
                continue
            bookmark = {
                "id": str(item["_id"]),
                "link": item["link"],
                "title": item.get("title", ""),
                "excerpt": item.get("excerpt", ""),
                "tags": item.get("tags") or [],
                "created": created,
            }
            bookmarks.append(bookmark)
            if created and (latest_created is None or created > latest_created):
                latest_created = created

        logger.info("raindrop_poll_done", count=len(bookmarks), latest=latest_created)
        return PollBookmarksResult(bookmarks=bookmarks, latest_created=latest_created)
