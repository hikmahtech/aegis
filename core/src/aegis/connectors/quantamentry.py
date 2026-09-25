"""Quantamentry API client: country policy-credibility scores and the
central-bank calendar, for Raphael's world watch (#676).

Auth is the ``X-API-Key`` header. No error message ever carries the key.
"""

from __future__ import annotations

import time

import httpx

from aegis.connectors._base import HTTPConnector


class QuantamentryError(RuntimeError):
    """Quantamentry could not answer. The message is safe to show anywhere."""


class QuantamentryClient(HTTPConnector):
    connector_name = "quantamentry"

    def __init__(self, url: str, api_key: str, *, timeout: float = 30.0, db_pool=None) -> None:
        super().__init__(timeout=timeout, db_pool=db_pool)
        self._url = url.rstrip("/")
        self._key = api_key

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._url,
            timeout=httpx.Timeout(self._timeout, connect=5.0),
            headers={"X-API-Key": self._key},
        )

    async def _get(self, path: str) -> dict | list:
        client = await self._ensure_client()
        t0 = time.monotonic()
        try:
            resp = await client.get(path)
        except httpx.HTTPError as exc:
            await self._record(path, "error", int((time.monotonic() - t0) * 1000), type(exc).__name__)
            raise QuantamentryError(f"{path}: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            await self._record(path, "error", int((time.monotonic() - t0) * 1000), f"HTTP {resp.status_code}")
            raise QuantamentryError(f"{path}: HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            await self._record(path, "error", int((time.monotonic() - t0) * 1000), "not JSON")
            raise QuantamentryError(f"{path}: the response was not JSON") from exc
        await self._record(path, "ok", int((time.monotonic() - t0) * 1000))
        return body

    async def scores(self) -> list[dict]:
        """Every currently scored country: composite, deltas, `regime_shift`."""
        body = await self._get("/api/scores")
        return [r for r in body if isinstance(r, dict)] if isinstance(body, list) else []

    async def cb_calendar(self) -> list[dict]:
        """One row per central bank the calendar covers: next/last meeting and
        the stance change between its last two scored statements."""
        body = await self._get("/api/cb-calendar")
        rows = body.get("rows") if isinstance(body, dict) else None
        return [r for r in rows or [] if isinstance(r, dict)]

    async def status(self) -> dict:
        """Freshness: `latest_score_date`, `max_score_age_days`, per source."""
        body = await self._get("/api/status.json")
        return body if isinstance(body, dict) else {}
