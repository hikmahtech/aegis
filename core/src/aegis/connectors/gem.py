"""GeM (Government e-Marketplace) bid search, read-only, for the tender watch
(#673).

GeM's public "all bids" page loads its results from `POST /all-bids-data`,
which takes a JSON `payload` and the page's CSRF token (`csrf_bd_gem_nk`) with
the page's session cookie. So a search first loads the page, once per client,
then posts. `robots.txt` allows both paths; the portal needs no login and shows
no captcha for them. Checked 2026-09-25.
"""

from __future__ import annotations

import json
import re
import time

import httpx

from aegis.connectors._base import HTTPConnector
from aegis.services.user_agent import bot_user_agent

BASE = "https://bidplus.gem.gov.in"
_TOKEN = re.compile(r"csrf_bd_gem_nk['\"]?\s*:\s*['\"]([0-9a-f]+)['\"]")


class GemError(RuntimeError):
    """GeM could not answer. The message is safe to show anywhere."""


class GemClient(HTTPConnector):
    connector_name = "gem"

    def __init__(self, *, timeout: float = 30.0, db_pool=None, base_url: str = BASE) -> None:
        super().__init__(timeout=timeout, db_pool=db_pool)
        self._base = base_url.rstrip("/")
        self._token: str | None = None

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base,
            headers={"User-Agent": bot_user_agent()},
            timeout=httpx.Timeout(self._timeout, connect=5.0),
            follow_redirects=False,
        )

    async def _ensure_token(self, client: httpx.AsyncClient) -> str:
        if self._token:
            return self._token
        resp = await client.get("/all-bids")
        if resp.status_code != 200:
            raise GemError(f"/all-bids: HTTP {resp.status_code}")
        m = _TOKEN.search(resp.text)
        if not m:
            raise GemError("/all-bids: no search token on the page")
        self._token = m.group(1)
        return self._token

    async def search(self, keyword: str, *, page: int = 1) -> list[dict]:
        """Ongoing bids matching ``keyword``, newest first, ten a page."""
        client = await self._ensure_client()
        path = "/all-bids-data"
        t0 = time.monotonic()
        payload = {
            "param": {"searchBid": keyword, "searchType": "fullText"},
            "filter": {
                "bidStatusType": "ongoing_bids",
                "byType": "all",
                "highBidValue": "",
                "byEndDate": {"from": "", "to": ""},
                "sort": "Bid-Start-Date-Latest",
            },
            "page": page,
        }
        try:
            token = await self._ensure_token(client)
            resp = await client.post(
                path,
                data={"payload": json.dumps(payload), "csrf_bd_gem_nk": token},
                headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{self._base}/all-bids"},
            )
        except httpx.HTTPError as exc:
            await self._record(path, "error", int((time.monotonic() - t0) * 1000), type(exc).__name__)
            raise GemError(f"{path}: {type(exc).__name__}") from exc
        except GemError as exc:
            await self._record(path, "error", int((time.monotonic() - t0) * 1000), str(exc))
            raise
        if resp.status_code != 200:
            await self._record(path, "error", int((time.monotonic() - t0) * 1000), f"HTTP {resp.status_code}")
            raise GemError(f"{path}: HTTP {resp.status_code}")
        try:
            body = resp.json() or {}
            docs = body["response"]["response"]["docs"]
        except (ValueError, KeyError, TypeError) as exc:
            await self._record(path, "error", int((time.monotonic() - t0) * 1000), "unexpected shape")
            raise GemError(f"{path}: the response was not the expected shape") from exc
        await self._record(path, "ok", int((time.monotonic() - t0) * 1000))
        return [d for d in docs if isinstance(d, dict)]
