"""Market-data chat tools — quotes, the configured index overview, finance news.

The first two read `ctx.finance_connector` (keyless web providers); the third
goes through the same SearXNG `search_connector` the research tools use, with a
finance-tuned query. All three are read-only.
"""

from __future__ import annotations

import json

import asyncpg
import structlog

from aegis.errors import error_text
from aegis.services.tools.base import ToolContext
from aegis.services.tools.registry import aegis_tool

logger = structlog.get_logger()


@aegis_tool
async def _exec_get_quote(pool: asyncpg.Pool, ctx: ToolContext, *, symbols: list[str]) -> str:
    """Get the current price and day change for one or more ticker symbols (stocks, ETFs, indices, crypto — provider-dependent). Max 10 symbols per call.

    Args:
        symbols: Ticker symbols, e.g. ["AAPL", "^NSEI", "BTC-USD"]. Max 10.
    """
    if not ctx.finance_connector:
        return json.dumps({"error": "Finance connector not available"})
    symbols = symbols or []
    if isinstance(symbols, str):
        symbols = symbols.split(",")
    symbols = [str(s).strip() for s in symbols if str(s).strip()]
    if not symbols:
        return json.dumps({"error": "symbols is required"})
    try:
        quotes = await ctx.finance_connector.get_quotes(symbols)
    except Exception as exc:
        logger.warning("get_quote_failed", error=error_text(exc, 500))
        return json.dumps({"error": f"quote lookup failed: {error_text(exc)}"})
    return json.dumps(quotes, default=str)


@aegis_tool
async def _exec_get_market_overview(pool: asyncpg.Pool, ctx: ToolContext) -> str:
    """Get current quotes for the configured market-overview indices (e.g. S&P 500, NASDAQ, NIFTY 50)."""
    if not ctx.finance_connector:
        return json.dumps({"error": "Finance connector not available"})
    try:
        quotes = await ctx.finance_connector.get_overview()
    except Exception as exc:
        logger.warning("get_market_overview_failed", error=error_text(exc, 500))
        return json.dumps({"error": f"market overview failed: {error_text(exc)}"})
    return json.dumps(quotes, default=str)


@aegis_tool
async def _exec_get_finance_news(
    pool: asyncpg.Pool, ctx: ToolContext, *, query: str, limit: int | None = None
) -> str:
    """Search recent finance/market news on a topic, company, or ticker via web search.

    Args:
        query: What to look up, e.g. a company, ticker, or market theme.
        limit: Number of results (default 10, max 20).

    Returns:
        Finance-tuned web news search over the same SearXNG SearchConnector
        that backs `research_topic`.
    """
    if not ctx.search_connector:
        return json.dumps({"error": "Search connector not available"})
    query = str(query or "").strip()
    if not query:
        return json.dumps({"error": "query is required"})
    limit = min(int(limit or 10), 20)
    try:
        results = await ctx.search_connector.search(
            f"{query} stock market finance", categories="news", limit=limit
        )
    except Exception as exc:
        logger.warning("get_finance_news_failed", error=error_text(exc, 500))
        return json.dumps({"error": f"news search failed: {error_text(exc)}"})
    return json.dumps({"query": query, "results": results})
