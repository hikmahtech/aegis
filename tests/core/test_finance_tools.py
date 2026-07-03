"""Tests for finance chat tools: get_quote, get_market_overview, get_finance_news."""

import json
from unittest.mock import AsyncMock

from aegis.services.chat import ToolContext, _execute_tool

QUOTE = {
    "symbol": "AAPL",
    "price": 213.55,
    "change": 3.55,
    "change_percent": 1.69,
    "currency": "USD",
    "as_of": "2026-07-02T20:00:00+00:00",
}


# --- get_quote ---


async def test_get_quote():
    pool = AsyncMock()
    fin = AsyncMock()
    fin.get_quotes = AsyncMock(return_value=[QUOTE])
    ctx = ToolContext(finance_connector=fin)
    result = await _execute_tool(pool, "get_quote", {"symbols": ["AAPL"]}, ctx=ctx)
    data = json.loads(result)
    assert data == [QUOTE]
    fin.get_quotes.assert_awaited_once_with(["AAPL"])


async def test_get_quote_accepts_comma_string():
    pool = AsyncMock()
    fin = AsyncMock()
    fin.get_quotes = AsyncMock(return_value=[])
    ctx = ToolContext(finance_connector=fin)
    await _execute_tool(pool, "get_quote", {"symbols": "AAPL, MSFT"}, ctx=ctx)
    fin.get_quotes.assert_awaited_once_with(["AAPL", "MSFT"])


async def test_get_quote_no_connector():
    pool = AsyncMock()
    ctx = ToolContext(finance_connector=None)
    result = await _execute_tool(pool, "get_quote", {"symbols": ["AAPL"]}, ctx=ctx)
    assert "error" in json.loads(result)


async def test_get_quote_missing_symbols():
    pool = AsyncMock()
    fin = AsyncMock()
    ctx = ToolContext(finance_connector=fin)
    result = await _execute_tool(pool, "get_quote", {}, ctx=ctx)
    assert "error" in json.loads(result)
    result = await _execute_tool(pool, "get_quote", {"symbols": [" ", ""]}, ctx=ctx)
    assert "error" in json.loads(result)


async def test_get_quote_connector_exception_becomes_error_envelope():
    pool = AsyncMock()
    fin = AsyncMock()
    fin.get_quotes = AsyncMock(side_effect=RuntimeError("provider down"))
    ctx = ToolContext(finance_connector=fin)
    result = await _execute_tool(pool, "get_quote", {"symbols": ["AAPL"]}, ctx=ctx)
    data = json.loads(result)
    assert "provider down" in data["error"]


# --- get_market_overview ---


async def test_get_market_overview():
    pool = AsyncMock()
    fin = AsyncMock()
    fin.get_overview = AsyncMock(return_value=[QUOTE, {"symbol": "^NSEI", "error": "timeout"}])
    ctx = ToolContext(finance_connector=fin)
    result = await _execute_tool(pool, "get_market_overview", {}, ctx=ctx)
    data = json.loads(result)
    assert len(data) == 2
    assert data[0]["symbol"] == "AAPL"
    assert data[1]["error"] == "timeout"
    fin.get_overview.assert_awaited_once()


async def test_get_market_overview_no_connector():
    pool = AsyncMock()
    ctx = ToolContext(finance_connector=None)
    result = await _execute_tool(pool, "get_market_overview", {}, ctx=ctx)
    assert "error" in json.loads(result)


# --- get_finance_news ---


async def test_get_finance_news():
    pool = AsyncMock()
    search = AsyncMock()
    search.search = AsyncMock(
        return_value=[{"title": "Apple hits high", "url": "https://x", "content": "..."}]
    )
    ctx = ToolContext(search_connector=search)
    result = await _execute_tool(pool, "get_finance_news", {"query": "AAPL"}, ctx=ctx)
    data = json.loads(result)
    assert data["query"] == "AAPL"
    assert len(data["results"]) == 1
    # Finance-tuned query against the news category of SearXNG.
    call = search.search.await_args
    assert "AAPL" in call.args[0]
    assert "finance" in call.args[0]
    assert call.kwargs["categories"] == "news"
    assert call.kwargs["limit"] == 10


async def test_get_finance_news_limit_capped():
    pool = AsyncMock()
    search = AsyncMock()
    search.search = AsyncMock(return_value=[])
    ctx = ToolContext(search_connector=search)
    await _execute_tool(pool, "get_finance_news", {"query": "nifty", "limit": 99}, ctx=ctx)
    assert search.search.await_args.kwargs["limit"] == 20


async def test_get_finance_news_missing_query():
    pool = AsyncMock()
    ctx = ToolContext(search_connector=AsyncMock())
    result = await _execute_tool(pool, "get_finance_news", {}, ctx=ctx)
    assert "error" in json.loads(result)


async def test_get_finance_news_no_search_connector():
    pool = AsyncMock()
    ctx = ToolContext(search_connector=None)
    result = await _execute_tool(pool, "get_finance_news", {"query": "gold"}, ctx=ctx)
    assert "error" in json.loads(result)


async def test_get_finance_news_search_error_envelope():
    pool = AsyncMock()
    search = AsyncMock()
    search.search = AsyncMock(side_effect=RuntimeError("searxng down"))
    ctx = ToolContext(search_connector=search)
    result = await _execute_tool(pool, "get_finance_news", {"query": "gold"}, ctx=ctx)
    data = json.loads(result)
    assert "searxng down" in data["error"]
