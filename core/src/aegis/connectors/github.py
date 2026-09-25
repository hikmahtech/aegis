"""GitHub search API client, read-only: new repositories by topic (#677's
weekly rising repos) and issue search (the `github_issues` tool).

A token is optional. Without one GitHub allows 10 searches a minute, which a
weekly run over a handful of topics and a chat tool stay well inside. No error
message ever carries the token.
"""

from __future__ import annotations

import time

import httpx

from aegis.connectors._base import HTTPConnector
from aegis.services.user_agent import bot_user_agent

API = "https://api.github.com"


class GitHubError(RuntimeError):
    """GitHub could not answer. The message is safe to show anywhere."""


class GitHubClient(HTTPConnector):
    connector_name = "github"

    def __init__(self, token: str = "", *, timeout: float = 20.0, db_pool=None, base_url: str = API) -> None:
        super().__init__(timeout=timeout, db_pool=db_pool)
        self._token = token
        self._base = base_url.rstrip("/")

    def _build_client(self) -> httpx.AsyncClient:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": bot_user_agent(),
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return httpx.AsyncClient(
            base_url=self._base, headers=headers, timeout=httpx.Timeout(self._timeout, connect=5.0)
        )

    async def _search(self, kind: str, q: str, sort: str, per_page: int) -> list[dict]:
        client = await self._ensure_client()
        path = f"/search/{kind}"
        t0 = time.monotonic()
        try:
            resp = await client.get(
                path, params={"q": q, "sort": sort, "order": "desc", "per_page": max(1, min(per_page, 50))}
            )
        except httpx.HTTPError as exc:
            await self._record(path, "error", int((time.monotonic() - t0) * 1000), type(exc).__name__)
            raise GitHubError(f"{path}: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            # 403/429 with a zero remaining budget is the rate limit.
            limited = resp.headers.get("x-ratelimit-remaining") == "0"
            detail = "rate limited" if limited else f"HTTP {resp.status_code}"
            await self._record(path, "error", int((time.monotonic() - t0) * 1000), detail)
            raise GitHubError(f"{path}: {detail}")
        try:
            items = (resp.json() or {}).get("items") or []
        except ValueError as exc:
            await self._record(path, "error", int((time.monotonic() - t0) * 1000), "not JSON")
            raise GitHubError(f"{path}: the response was not JSON") from exc
        await self._record(path, "ok", int((time.monotonic() - t0) * 1000))
        return [i for i in items if isinstance(i, dict)]

    async def search_repos(self, q: str, *, sort: str = "stars", per_page: int = 10) -> list[dict]:
        return await self._search("repositories", q, sort, per_page)

    async def search_issues(self, q: str, *, sort: str = "reactions", per_page: int = 10) -> list[dict]:
        return await self._search("issues", q, sort, per_page)
