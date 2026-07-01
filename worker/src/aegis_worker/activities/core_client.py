"""HTTP client for Core API — used by worker activities."""

from __future__ import annotations

import httpx
import structlog

logger = structlog.get_logger()


class CoreClient:
    """Async HTTP client to the Core API."""

    def __init__(self, base_url: str, api_key: str = "", timeout: int = 30):
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"X-API-Key": api_key} if api_key else {},
            timeout=timeout,
        )

    async def get(self, path: str, **kwargs) -> httpx.Response:
        return await self._client.get(path, **kwargs)

    async def post(self, path: str, **kwargs) -> httpx.Response:
        return await self._client.post(path, **kwargs)

    async def close(self):
        await self._client.aclose()
