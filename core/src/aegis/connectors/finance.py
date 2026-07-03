"""Finance connector — provider-agnostic web market data (quotes).

Ships two keyless built-in providers:

- ``yahoo`` — Yahoo Finance chart API (JSON), one request per symbol.
- ``stooq`` — Stooq quote CSV endpoint, one request per symbol.

The provider seam is the module-level ``_PROVIDERS`` registry: each entry is
an async function ``(client, symbol, api_key) -> quote dict``. Adding a new
(possibly API-key) provider is one function + one registry entry — the
``finance_api_key`` setting is already threaded through for it.

Every quote is a small envelope so partial failures degrade per-symbol:

    {"symbol", "price", "change", "change_percent", "currency", "as_of"}
    or {"symbol", "error"}
"""

from __future__ import annotations

import asyncio
import csv
import io
import time
from datetime import UTC, datetime

import httpx
import structlog

from aegis.connectors._base import HTTPConnector

logger = structlog.get_logger()

DEFAULT_INDICES = "^GSPC,^IXIC,^NSEI"

_MAX_SYMBOLS = 10

# Yahoo (and some other providers) reject default python-httpx user agents.
_USER_AGENT = "Mozilla/5.0 (compatible; aegis-finance/1.0)"

_YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_STOOQ_QUOTE_URL = "https://stooq.com/q/l/"


def _pct(change: float | None, base: float | None) -> float | None:
    if change is None or not base:
        return None
    return round(change / base * 100, 2)


async def _quote_yahoo(client: httpx.AsyncClient, symbol: str, api_key: str) -> dict:
    """Quote via the Yahoo Finance v8 chart endpoint (keyless JSON)."""
    resp = await client.get(
        _YAHOO_CHART_URL.format(symbol=symbol),
        params={"interval": "1d", "range": "1d"},
    )
    if resp.status_code >= 400:
        return {"symbol": symbol, "error": f"HTTP {resp.status_code}"}
    chart = (resp.json() or {}).get("chart") or {}
    error = chart.get("error")
    result = chart.get("result") or []
    if error or not result:
        detail = (error or {}).get("description") or (error or {}).get("code") or "no data"
        return {"symbol": symbol, "error": str(detail)}
    meta = result[0].get("meta") or {}
    price = meta.get("regularMarketPrice")
    if not isinstance(price, (int, float)):
        return {"symbol": symbol, "error": "no price in response"}
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    change = round(price - prev, 4) if isinstance(prev, (int, float)) else None
    as_of = None
    ts = meta.get("regularMarketTime")
    if isinstance(ts, (int, float)):
        as_of = datetime.fromtimestamp(ts, tz=UTC).isoformat()
    return {
        "symbol": meta.get("symbol") or symbol,
        "price": price,
        "change": change,
        "change_percent": _pct(change, prev),
        "currency": meta.get("currency"),
        "as_of": as_of,
    }


async def _quote_stooq(client: httpx.AsyncClient, symbol: str, api_key: str) -> dict:
    """Quote via the Stooq CSV endpoint (keyless).

    ``f=sd2t2ohlcv`` → Symbol,Date,Time,Open,High,Low,Close,Volume. Stooq has
    no previous-close field in this format, so change is measured vs the open.
    Note: stooq intermittently fronts these endpoints with a JS browser check;
    when that happens the non-CSV body is surfaced as a per-symbol error.
    """
    resp = await client.get(
        _STOOQ_QUOTE_URL,
        params={"s": symbol.lower(), "f": "sd2t2ohlcv", "h": "", "e": "csv"},
    )
    if resp.status_code >= 400:
        return {"symbol": symbol, "error": f"HTTP {resp.status_code}"}
    body = resp.text
    if "Symbol" not in body.split("\n", 1)[0]:
        return {"symbol": symbol, "error": "unexpected response (not CSV)"}
    rows = list(csv.DictReader(io.StringIO(body)))
    if not rows:
        return {"symbol": symbol, "error": "empty response"}
    row = rows[0]
    close_raw = row.get("Close", "")
    if not close_raw or close_raw == "N/D":
        return {"symbol": symbol, "error": "no data (N/D)"}
    try:
        price = float(close_raw)
        open_ = float(row["Open"]) if row.get("Open") not in (None, "", "N/D") else None
    except ValueError:
        return {"symbol": symbol, "error": f"unparseable quote: {close_raw!r}"}
    change = round(price - open_, 4) if open_ is not None else None
    as_of = f"{row.get('Date', '')} {row.get('Time', '')}".strip() or None
    return {
        "symbol": row.get("Symbol") or symbol.upper(),
        "price": price,
        "change": change,
        "change_percent": _pct(change, open_),
        "currency": None,
        "as_of": as_of,
    }


# Provider seam — add a provider by registering one async function here.
_PROVIDERS = {
    "yahoo": _quote_yahoo,
    "stooq": _quote_stooq,
}


class FinanceConnector(HTTPConnector):
    """Web market-data client. Provider selected by config; keyless defaults."""

    connector_name = "finance"

    def __init__(
        self,
        provider: str = "yahoo",
        api_key: str = "",
        indices: str = DEFAULT_INDICES,
        timeout: float = 10.0,
        db_pool=None,
    ):
        super().__init__(timeout=timeout, db_pool=db_pool)
        self._provider = (provider or "yahoo").strip().lower()
        self._api_key = api_key or ""
        self._indices = indices or DEFAULT_INDICES

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout, connect=5.0),
            headers={"User-Agent": _USER_AGENT},
            follow_redirects=True,
        )

    async def get_quotes(self, symbols: list[str]) -> list[dict]:
        """Current quotes for up to 10 symbols; per-symbol error envelopes."""
        clean: list[str] = []
        for s in symbols or []:
            s = str(s).strip().upper()
            if s and s not in clean:
                clean.append(s)
        clean = clean[:_MAX_SYMBOLS]
        if not clean:
            return []

        fetch = _PROVIDERS.get(self._provider)
        if fetch is None:
            return [
                {"symbol": s, "error": f"unknown finance provider '{self._provider}'"}
                for s in clean
            ]

        client = await self._ensure_client()
        t0 = time.monotonic()
        results = list(
            await asyncio.gather(*(self._one_quote(fetch, client, s) for s in clean))
        )
        latency = int((time.monotonic() - t0) * 1000)
        errors = [r["error"] for r in results if r.get("error")]
        status = "ok" if len(errors) < len(results) else "error"
        await self._record("get_quotes", status, latency, errors[0] if errors else None)
        return results

    async def _one_quote(self, fetch, client: httpx.AsyncClient, symbol: str) -> dict:
        try:
            return await fetch(client, symbol, self._api_key)
        except httpx.TimeoutException:
            return {"symbol": symbol, "error": "timeout"}
        except Exception as exc:  # noqa: BLE001 — one bad symbol must not sink the batch
            logger.warning(
                "finance_quote_failed",
                provider=self._provider,
                symbol=symbol,
                error=str(exc)[:200],
            )
            return {"symbol": symbol, "error": str(exc)[:200]}

    async def get_overview(self) -> list[dict]:
        """Quotes for the configured market-overview indices."""
        symbols = [s.strip() for s in self._indices.split(",") if s.strip()]
        return await self.get_quotes(symbols)
