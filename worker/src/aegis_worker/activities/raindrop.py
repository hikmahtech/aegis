"""RaindropActivities — bookmark poll + channel cursor helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog
from temporalio import activity

logger = structlog.get_logger()

_BASE = "https://api.raindrop.io/rest/v1"
_PERPAGE = 50
# ponytail: runaway guard — 10 pages * 50/page = 500 bookmarks per poll is far
# beyond any real burst; stops a misbehaving API response from looping forever.
_MAX_PAGES = 10


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

        base_params: dict[str, Any] = {"sort": "-created", "perpage": _PERPAGE}
        if input.since_cursor:
            # Raindrop's `created:>` search operator is date-granular — full
            # ISO timestamps return 0 rows. Strip to YYYY-MM-DD here; the
            # post-fetch filter below applies the full-ISO precision.
            date_part = input.since_cursor[:10]
            base_params["search"] = f"created:>{date_part}"

        client = self.http_client or httpx.AsyncClient()
        bookmarks: list[dict] = []
        latest_created: str | None = None
        pages_fetched = 0

        try:
            for page in range(_MAX_PAGES):
                resp = await client.get(
                    f"{_BASE}/raindrops/0",
                    headers={"Authorization": f"Bearer {self.raindrop_api_token}"},
                    params={**base_params, "page": page},
                )
                resp.raise_for_status()
                pages_fetched += 1

                items = resp.json().get("items") or []
                if not items:
                    break

                reached_cursor = False
                for item in items:
                    created = item.get("created", "")
                    # API filter is date-granular, so same-day already-ingested
                    # items come back on every tick. Results are sorted
                    # -created (newest first) both within and across pages, so
                    # the first item at/before the cursor means every item
                    # after it (this page and any further page) is too —
                    # safe to stop the whole pagination loop right here.
                    if input.since_cursor and created and created <= input.since_cursor:
                        reached_cursor = True
                        break
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

                if reached_cursor or len(items) < _PERPAGE:
                    break
            else:
                logger.warning("raindrop_poll_hit_page_cap", pages=_MAX_PAGES)
        finally:
            if self.http_client is None:
                await client.aclose()

        logger.info(
            "raindrop_poll_done",
            count=len(bookmarks),
            latest=latest_created,
            pages=pages_fetched,
        )
        return PollBookmarksResult(bookmarks=bookmarks, latest_created=latest_created)
