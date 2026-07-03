"""Tests for FinanceConnector — yahoo + stooq providers via respx."""

import httpx
import pytest
import respx
from aegis.connectors.finance import DEFAULT_INDICES, FinanceConnector

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/AAPL"
STOOQ_URL = "https://stooq.com/q/l/"


def _yahoo_payload(symbol="AAPL", price=213.55, prev=210.0, ts=1751500800):
    return {
        "chart": {
            "result": [
                {
                    "meta": {
                        "symbol": symbol,
                        "currency": "USD",
                        "regularMarketPrice": price,
                        "chartPreviousClose": prev,
                        "regularMarketTime": ts,
                    }
                }
            ],
            "error": None,
        }
    }


@pytest.fixture
def yahoo():
    return FinanceConnector(provider="yahoo")


@pytest.fixture
def stooq():
    return FinanceConnector(provider="stooq")


# --- yahoo ---


@respx.mock
async def test_yahoo_quote_parse(yahoo):
    respx.get(YAHOO_URL).mock(return_value=httpx.Response(200, json=_yahoo_payload()))
    quotes = await yahoo.get_quotes(["AAPL"])
    assert len(quotes) == 1
    q = quotes[0]
    assert q["symbol"] == "AAPL"
    assert q["price"] == 213.55
    assert q["change"] == pytest.approx(3.55)
    assert q["change_percent"] == pytest.approx(1.69)
    assert q["currency"] == "USD"
    assert q["as_of"].startswith("2025") or q["as_of"].startswith("2026")
    assert "error" not in q


@respx.mock
async def test_yahoo_unknown_symbol_error_envelope(yahoo):
    respx.get("https://query1.finance.yahoo.com/v8/finance/chart/NOPE").mock(
        return_value=httpx.Response(
            404,
            json={"chart": {"result": None, "error": {"description": "No data found"}}},
        )
    )
    quotes = await yahoo.get_quotes(["NOPE"])
    assert quotes == [{"symbol": "NOPE", "error": "HTTP 404"}]


@respx.mock
async def test_yahoo_error_body_with_200(yahoo):
    respx.get("https://query1.finance.yahoo.com/v8/finance/chart/NOPE").mock(
        return_value=httpx.Response(
            200,
            json={"chart": {"result": None, "error": {"description": "delisted"}}},
        )
    )
    quotes = await yahoo.get_quotes(["nope"])
    assert quotes[0]["symbol"] == "NOPE"
    assert quotes[0]["error"] == "delisted"


@respx.mock
async def test_yahoo_timeout_error_envelope(yahoo):
    respx.get(YAHOO_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
    quotes = await yahoo.get_quotes(["AAPL"])
    assert quotes == [{"symbol": "AAPL", "error": "timeout"}]


@respx.mock
async def test_yahoo_partial_failure_keeps_good_quotes(yahoo):
    respx.get(YAHOO_URL).mock(return_value=httpx.Response(200, json=_yahoo_payload()))
    respx.get("https://query1.finance.yahoo.com/v8/finance/chart/BAD").mock(
        side_effect=httpx.ReadTimeout("slow")
    )
    quotes = await yahoo.get_quotes(["AAPL", "BAD"])
    assert quotes[0]["price"] == 213.55
    assert quotes[1] == {"symbol": "BAD", "error": "timeout"}


# --- stooq ---


@respx.mock
async def test_stooq_quote_parse(stooq):
    csv_body = (
        "Symbol,Date,Time,Open,High,Low,Close,Volume\n"
        "AAPL.US,2026-07-02,22:00:11,212.1,213.34,208.14,213.55,44840000\n"
    )
    route = respx.get(STOOQ_URL).mock(return_value=httpx.Response(200, text=csv_body))
    quotes = await stooq.get_quotes(["AAPL.US"])
    q = quotes[0]
    assert q["symbol"] == "AAPL.US"
    assert q["price"] == 213.55
    assert q["change"] == pytest.approx(1.45)
    assert q["change_percent"] == pytest.approx(0.68)
    assert q["as_of"] == "2026-07-02 22:00:11"
    # Stooq wants lowercase symbols in the query string.
    assert route.calls.last.request.url.params["s"] == "aapl.us"


@respx.mock
async def test_stooq_no_data_symbol(stooq):
    csv_body = "Symbol,Date,Time,Open,High,Low,Close,Volume\nNOPE,N/D,N/D,N/D,N/D,N/D,N/D,N/D\n"
    respx.get(STOOQ_URL).mock(return_value=httpx.Response(200, text=csv_body))
    quotes = await stooq.get_quotes(["NOPE"])
    assert quotes[0]["symbol"] == "NOPE"
    assert "N/D" in quotes[0]["error"]


@respx.mock
async def test_stooq_http_error_envelope(stooq):
    respx.get(STOOQ_URL).mock(return_value=httpx.Response(503))
    quotes = await stooq.get_quotes(["AAPL.US"])
    assert quotes == [{"symbol": "AAPL.US", "error": "HTTP 503"}]


# --- connector-level behavior ---


async def test_unknown_provider_error_envelope():
    conn = FinanceConnector(provider="bloomberg")
    quotes = await conn.get_quotes(["AAPL"])
    assert quotes[0]["symbol"] == "AAPL"
    assert "unknown finance provider" in quotes[0]["error"]


async def test_empty_symbols_returns_empty():
    conn = FinanceConnector()
    assert await conn.get_quotes([]) == []
    assert await conn.get_quotes(["", "  "]) == []


@respx.mock
async def test_symbols_deduped_and_capped_at_ten():
    conn = FinanceConnector(provider="yahoo")
    route = respx.get(url__regex=r"https://query1\.finance\.yahoo\.com/v8/finance/chart/.*").mock(
        return_value=httpx.Response(200, json=_yahoo_payload())
    )
    symbols = [f"S{i}" for i in range(15)] + ["S0", "s1"]
    quotes = await conn.get_quotes(symbols)
    assert len(quotes) == 10
    assert route.call_count == 10


@respx.mock
async def test_get_overview_uses_configured_indices():
    conn = FinanceConnector(provider="yahoo", indices=" ^GSPC , ^NSEI ")
    route = respx.get(url__regex=r"https://query1\.finance\.yahoo\.com/v8/finance/chart/.*").mock(
        return_value=httpx.Response(200, json=_yahoo_payload(symbol="^GSPC"))
    )
    quotes = await conn.get_overview()
    assert len(quotes) == 2
    assert route.call_count == 2
    called = {c.request.url.path.rsplit("/", 1)[-1] for c in route.calls}
    assert called == {"^GSPC", "^NSEI"}


def test_default_indices():
    conn = FinanceConnector(indices="")
    assert conn._indices == DEFAULT_INDICES
