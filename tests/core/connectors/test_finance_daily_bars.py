"""Yahoo daily bars for the trading desk (spec §3 step 3)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
import pytest
import respx
from aegis.connectors.finance import FinanceConnector

CHART = r"https://query1\.finance\.yahoo\.com/v8/finance/chart/"


def _ts(day: int) -> int:
    """Yahoo stamps an NSE bar at the 09:15 IST open, 03:45 UTC."""
    return int(datetime(2026, 9, day, 3, 45, tzinfo=UTC).timestamp())


BODY = {
    "chart": {
        "result": [
            {
                "meta": {"gmtoffset": 19800},
                "timestamp": [_ts(14), _ts(15), _ts(16)],
                "events": {
                    "splits": {str(_ts(15)): {"date": _ts(15), "numerator": 2.0, "denominator": 1.0}},
                    "dividends": {str(_ts(16)): {"date": _ts(16), "amount": 1.5}},
                },
                "indicators": {"quote": [{"open": [99.0, None, 50.5], "close": [100.0, None, 51.0]}]},
            }
        ],
        "error": None,
    }
}


@respx.mock
async def test_daily_bars_parse_closes_splits_and_dividends():
    route = respx.get(url__regex=CHART + r"TCS\.NS").mock(return_value=httpx.Response(200, json=BODY))
    bars = await FinanceConnector().daily_bars("TCS.NS", date(2026, 9, 14), date(2026, 9, 16))
    assert bars == [
        {"day": date(2026, 9, 14), "open": 99.0, "close": 100.0, "split_ratio": None, "dividend": None},
        {"day": date(2026, 9, 15), "open": None, "close": None, "split_ratio": 2.0, "dividend": None},
        {"day": date(2026, 9, 16), "open": 50.5, "close": 51.0, "split_ratio": None, "dividend": 1.5},
    ]
    params = route.calls.last.request.url.params
    assert params["interval"] == "1d" and params["events"] == "div,split"
    assert int(params["period1"]) == int(datetime(2026, 9, 14, tzinfo=UTC).timestamp())
    assert int(params["period2"]) == int(datetime(2026, 9, 17, tzinfo=UTC).timestamp())


@respx.mock
async def test_daily_bars_for_an_unknown_symbol_are_empty():
    respx.get(url__regex=CHART + r"NOPE\.NS").mock(
        return_value=httpx.Response(404, json={"chart": {"result": None, "error": {"code": "Not Found"}}})
    )
    assert await FinanceConnector().daily_bars("NOPE.NS", date(2026, 9, 14), date(2026, 9, 16)) == []


@respx.mock
async def test_daily_bars_raise_on_a_server_error():
    respx.get(url__regex=CHART + r"TCS\.NS").mock(return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        await FinanceConnector().daily_bars("TCS.NS", date(2026, 9, 14), date(2026, 9, 16))


@respx.mock
async def test_a_session_still_open_has_a_settled_open_and_a_live_close():
    """Yahoo prices a day in progress with the opening print, which is final
    from 09:15, and a `close` that is really the last trade and still moving.
    That asymmetry is the whole reason the desk can fill the same morning — and
    the reason `_store_bars` refuses to keep that close."""
    body = {
        "chart": {
            "result": [
                {
                    "meta": {"gmtoffset": 19800},
                    "timestamp": [_ts(15)],
                    "indicators": {"quote": [{"open": [23576.15], "close": [23118.6]}]},
                }
            ],
            "error": None,
        }
    }
    respx.get(url__regex=CHART + r"LIVE\.NS").mock(return_value=httpx.Response(200, json=body))
    bars = await FinanceConnector().daily_bars("LIVE.NS", date(2026, 9, 15), date(2026, 9, 15))
    assert (bars[0]["open"], bars[0]["close"]) == (23576.15, 23118.6)


@respx.mock
async def test_a_day_the_market_was_shut_has_neither_price():
    """A holiday comes back as a bar, not as an absence — both fields None. It
    must not become a market day on the strength of existing."""
    body = {
        "chart": {
            "result": [
                {
                    "meta": {"gmtoffset": 19800},
                    "timestamp": [_ts(14)],
                    "indicators": {"quote": [{"open": [None], "close": [None]}]},
                }
            ],
            "error": None,
        }
    }
    respx.get(url__regex=CHART + r"SHUT\.NS").mock(return_value=httpx.Response(200, json=body))
    bars = await FinanceConnector().daily_bars("SHUT.NS", date(2026, 9, 14), date(2026, 9, 14))
    assert (bars[0]["open"], bars[0]["close"]) == (None, None)
