"""RSS/Atom feed fetch via feedparser. Runs in asyncio.to_thread since feedparser is sync."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import structlog
from temporalio import activity

logger = structlog.get_logger()


@dataclass
class FetchFeedInput:
    url: str
    since_cursor: str | None = None  # ISO timestamp


@dataclass
class FetchFeedResult:
    entries: list[dict] = field(default_factory=list)
    latest_published: str | None = None


@dataclass
class RssActivities:
    db_pool: Any

    @activity.defn
    async def fetch_feed(self, input: FetchFeedInput) -> FetchFeedResult:
        """Parse a feed URL. Entry dicts: {id, title, link, summary, published}."""

        def _sync() -> FetchFeedResult:
            import feedparser

            parsed = feedparser.parse(input.url)
            entries: list[dict] = []
            latest: str | None = None
            for e in parsed.entries:
                # Prefer published_parsed (9-tuple) -> ISO; fall back to raw
                # published string. Cursor comparisons elsewhere (Raindrop's
                # `last_cursor`) are tz-aware (Z suffix), so build tz-aware
                # ISO strings here too — naive vs tz-aware lexicographic
                # compare is silently broken otherwise.
                published_iso = ""
                if getattr(e, "published_parsed", None):
                    import datetime as _dt

                    published_iso = (
                        _dt.datetime(*e.published_parsed[:6])
                        .replace(tzinfo=_dt.UTC)
                        .isoformat()
                    )
                elif getattr(e, "updated_parsed", None):
                    import datetime as _dt

                    published_iso = (
                        _dt.datetime(*e.updated_parsed[:6])
                        .replace(tzinfo=_dt.UTC)
                        .isoformat()
                    )
                else:
                    published_iso = getattr(e, "published", "") or getattr(e, "updated", "")

                if input.since_cursor and published_iso and published_iso <= input.since_cursor:
                    continue

                entries.append(
                    {
                        "id": getattr(e, "id", "") or getattr(e, "link", ""),
                        "title": getattr(e, "title", ""),
                        "link": getattr(e, "link", ""),
                        "summary": getattr(e, "summary", "")[:500]
                        if getattr(e, "summary", "")
                        else "",
                        "published": published_iso,
                    }
                )
                if published_iso and (latest is None or published_iso > latest):
                    latest = published_iso

            return FetchFeedResult(entries=entries, latest_published=latest)

        return await asyncio.to_thread(_sync)
