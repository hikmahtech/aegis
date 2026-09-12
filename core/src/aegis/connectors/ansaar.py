"""ansaar-data API client: the trading system's read side (trading desk spec §12).

Auth is a 15-minute client token from ``POST /api/auth/client-token``, given the
service secret, fetched once per client. A run makes one client, so one token.
Never the admin login (ansaar-data #25). No error message ever carries the
secret: it only travels in the token request's JSON body.
"""

from __future__ import annotations

import time
from datetime import date
from urllib.parse import quote

import httpx

from aegis.connectors._base import HTTPConnector


class AnsaarError(RuntimeError):
    """ansaar could not answer. The message is safe to show anywhere."""


class AnsaarClient(HTTPConnector):
    connector_name = "ansaar"

    def __init__(self, url: str, service_secret: str, *, timeout: float = 20.0, db_pool=None) -> None:
        super().__init__(timeout=timeout, db_pool=db_pool)
        self._url = url.rstrip("/")
        self._secret = service_secret
        self._token: str | None = None

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self._url, timeout=httpx.Timeout(self._timeout, connect=5.0))

    async def _get(self, path: str, params: dict) -> dict:
        client = await self._ensure_client()
        t0 = time.monotonic()
        try:
            if self._token is None:
                resp = await client.post("/api/auth/client-token", json={"serviceSecret": self._secret})
                if resp.status_code != 200:
                    raise AnsaarError(f"client-token: HTTP {resp.status_code}")
                self._token = (resp.json() or {}).get("token")
                if not self._token:
                    raise AnsaarError("client-token: no token in the response")
            resp = await client.get(path, params=params, headers={"Authorization": f"Bearer {self._token}"})
        except (httpx.HTTPError, ValueError) as exc:
            # A ValueError here is the token response's body not being JSON. The
            # message names the path and the problem, and the secret never
            # leaves the token request's body.
            await self._record(path, "error", int((time.monotonic() - t0) * 1000), type(exc).__name__)
            raise AnsaarError(f"{path}: {type(exc).__name__}") from exc
        except AnsaarError as exc:
            await self._record(path, "error", int((time.monotonic() - t0) * 1000), str(exc))
            raise
        if resp.status_code != 200:
            await self._record(path, "error", int((time.monotonic() - t0) * 1000), f"HTTP {resp.status_code}")
            raise AnsaarError(f"{path}: HTTP {resp.status_code}")
        try:
            body = resp.json() or {}
        except ValueError as exc:
            await self._record(path, "error", int((time.monotonic() - t0) * 1000), type(exc).__name__)
            raise AnsaarError(f"{path}: the response was not JSON") from exc
        await self._record(path, "ok", int((time.monotonic() - t0) * 1000))
        return body

    async def decisions(self, day: date) -> tuple[list[dict], dict]:
        """``trade_decisions`` rows for exactly ``day`` (ansaar-data #30) and the meta."""
        body = await self._get("/api/execution/trade-decisions", {"date": day.isoformat()})
        return list(body.get("data") or []), dict(body.get("meta") or {})

    async def prices(self, symbol: str, asset_class: str, start: date, end: date) -> list[dict]:
        """Closes from ansaar's own price table, oldest first: the desk's fallback
        when Yahoo has no bar. The endpoints return newest first."""
        kind = "etfs" if asset_class == "etf" else "equities"
        path = (
            f"/api/etfs/{quote(symbol, safe='')}/prices"
            if kind == "etfs"
            else f"/api/equities/prices/{quote(symbol, safe='')}"
        )
        body = await self._get(path, {"from": start.isoformat(), "to": end.isoformat(), "limit": 1000})
        out = [
            {"day": date.fromisoformat(str(r["date"])[:10]), "close": float(r["close"]), "split_ratio": None, "dividend": None}
            for r in body.get("data") or []
            if r.get("close") is not None
        ]
        return sorted(out, key=lambda b: b["day"])
