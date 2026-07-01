"""Search connector — SearxNG meta-search wrapper."""

from __future__ import annotations

import httpx
import structlog

from aegis.connectors._base import HTTPConnector

logger = structlog.get_logger()


class SearchConnector(HTTPConnector):
    """SearxNG search client with a pooled httpx.AsyncClient.

    The client is lazily created on first use and reused across calls so
    repeated searches (e.g. `research_topic` chat tool firing several times
    per session) don't pay per-call TCP handshake costs.
    """

    connector_name = "search"

    def __init__(self, base_url: str = "http://localhost:8888", timeout: int = 30):
        super().__init__(timeout=timeout)
        self._base_url = base_url.rstrip("/")

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(self._timeout, connect=5.0),
        )

    async def search(self, query: str, categories: str = "general", limit: int = 10) -> list[dict]:
        """Search via SearxNG and return results."""
        client = await self._ensure_client()
        resp = await client.get(
            "/search",
            params={"q": query, "categories": categories, "format": "json"},
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])[:limit]
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "content": r.get("content", ""),
            }
            for r in results
        ]
