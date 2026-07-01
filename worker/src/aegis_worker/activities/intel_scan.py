"""IntelScanActivities — searxng queries for HN / news / finance intel scans."""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import structlog
from temporalio import activity

logger = structlog.get_logger()


@dataclass
class SearchSourceInput:
    source: str  # 'hn' | 'news' | 'finance'
    topics: list[str] = field(default_factory=list)
    max_results: int = 20


@dataclass
class SearchSourceResult:
    items: list[dict] = field(default_factory=list)
    source: str = ""


@dataclass
class IntelScanActivities:
    searxng_url: str
    http_client: httpx.AsyncClient | None = None

    def _build_query(self, source: str, topic: str) -> dict[str, str]:
        """Return query params dict for a given source + topic."""
        if source == "hn":
            return {"q": f"site:news.ycombinator.com {topic}", "format": "json"}
        if source == "finance":
            return {
                "q": f"{topic} site:ft.com OR site:reuters.com OR site:bloomberg.com",
                "categories": "news",
                "format": "json",
            }
        # default: news
        return {"q": topic, "categories": "news", "format": "json"}

    @activity.defn
    async def search_source(self, input: SearchSourceInput) -> SearchSourceResult:
        if not self.searxng_url:
            logger.warning("searxng_url_missing")
            return SearchSourceResult(items=[], source=input.source)

        client = self.http_client or httpx.AsyncClient()
        seen_urls: set[str] = set()
        merged: list[dict] = []

        try:
            for topic in input.topics:
                params = self._build_query(input.source, topic)
                resp = await client.get(f"{self.searxng_url}/search", params=params)
                resp.raise_for_status()
                data = resp.json()
                for r in data.get("results", []):
                    url = r.get("url", "")
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    merged.append(
                        {
                            "title": r.get("title", ""),
                            "url": url,
                            "snippet": (r.get("content", "") or "")[:500],
                            "source": input.source,
                            "published": r.get("publishedDate", ""),
                        }
                    )
        finally:
            if self.http_client is None:
                await client.aclose()

        trimmed = merged[: input.max_results]
        logger.info(
            "intel_scan_done",
            source=input.source,
            topics=input.topics,
            total=len(trimmed),
        )
        return SearchSourceResult(items=trimmed, source=input.source)
