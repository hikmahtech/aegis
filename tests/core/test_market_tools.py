"""Tests for market data chat tools: get_market_regime, get_top_forecasts, get_trade_decisions."""

import json
from unittest.mock import AsyncMock

from aegis.services.chat import ToolContext, _execute_tool


def _make_ctx(ch=None):
    return ToolContext(clickhouse_connector=ch)


# --- get_market_regime ---


async def test_get_market_regime():
    pool = AsyncMock()
    ch = AsyncMock()
    # First call: equity, second call: crypto
    ch.query = AsyncMock(
        side_effect=[
            [{"date": "2026-03-29", "close": 22000.5, "regime_label": "bull"}],
            [
                {
                    "date": "2026-03-29",
                    "fgi_value": 55,
                    "fgi_classification": "Neutral",
                    "total_market_cap_usd": 2.5e12,
                }
            ],
        ]
    )
    ctx = _make_ctx(ch)
    result = await _execute_tool(pool, "get_market_regime", {}, ctx=ctx)
    data = json.loads(result)
    assert "equity" in data
    assert "crypto" in data
    assert data["equity"]["regime"] == "bull"
    assert data["equity"]["index"] == "NIFTY 50"
    assert data["equity"]["close"] == 22000.5
    assert data["crypto"]["fear_greed"] == 55
    assert ch.query.call_count == 2


async def test_get_market_regime_no_connector():
    pool = AsyncMock()
    ctx = _make_ctx(None)
    result = await _execute_tool(pool, "get_market_regime", {}, ctx=ctx)
    data = json.loads(result)
    assert "error" in data


async def test_get_market_regime_empty_rows():
    pool = AsyncMock()
    ch = AsyncMock()
    ch.query = AsyncMock(side_effect=[[], []])
    ctx = _make_ctx(ch)
    result = await _execute_tool(pool, "get_market_regime", {}, ctx=ctx)
    data = json.loads(result)
    # Should still return structure, equity/crypto keys may be None or empty dict
    assert "equity" in data
    assert "crypto" in data


# --- get_top_forecasts ---


async def test_get_top_forecasts_equity():
    pool = AsyncMock()
    ch = AsyncMock()
    ch.query = AsyncMock(
        return_value=[
            {"symbol": "RELIANCE", "combined_forecast": 0.82, "close": 2800.0},
            {"symbol": "INFY", "combined_forecast": 0.71, "close": 1500.0},
        ]
    )
    ctx = _make_ctx(ch)
    result = await _execute_tool(pool, "get_top_forecasts", {"asset_class": "equity"}, ctx=ctx)
    data = json.loads(result)
    assert isinstance(data, list)
    assert len(data) == 2
    assert data[0]["symbol"] == "RELIANCE"
    ch.query.assert_called_once()


async def test_get_top_forecasts_equity_halal():
    pool = AsyncMock()
    ch = AsyncMock()
    ch.query = AsyncMock(return_value=[{"symbol": "INFY", "combined_forecast": 0.65}])
    ctx = _make_ctx(ch)
    result = await _execute_tool(
        pool,
        "get_top_forecasts",
        {"asset_class": "equity", "halal_only": True},
        ctx=ctx,
    )
    data = json.loads(result)
    assert isinstance(data, list)
    # Verify halal filter is included in the SQL
    call_args = ch.query.call_args
    sql = call_args[0][0]
    assert "is_halal" in sql


async def test_get_top_forecasts_direction_long():
    pool = AsyncMock()
    ch = AsyncMock()
    ch.query = AsyncMock(return_value=[])
    ctx = _make_ctx(ch)
    await _execute_tool(
        pool,
        "get_top_forecasts",
        {"asset_class": "equity", "direction": "long"},
        ctx=ctx,
    )
    call_args = ch.query.call_args
    sql = call_args[0][0]
    assert "combined_forecast > 0" in sql


async def test_get_top_forecasts_direction_short():
    pool = AsyncMock()
    ch = AsyncMock()
    ch.query = AsyncMock(return_value=[])
    ctx = _make_ctx(ch)
    await _execute_tool(
        pool,
        "get_top_forecasts",
        {"asset_class": "equity", "direction": "short"},
        ctx=ctx,
    )
    call_args = ch.query.call_args
    sql = call_args[0][0]
    assert "combined_forecast < 0" in sql


async def test_get_top_forecasts_missing_asset_class():
    pool = AsyncMock()
    ch = AsyncMock()
    ctx = _make_ctx(ch)
    result = await _execute_tool(pool, "get_top_forecasts", {}, ctx=ctx)
    data = json.loads(result)
    assert "error" in data


async def test_get_top_forecasts_invalid_asset_class():
    pool = AsyncMock()
    ch = AsyncMock()
    ctx = _make_ctx(ch)
    result = await _execute_tool(pool, "get_top_forecasts", {"asset_class": "bonds"}, ctx=ctx)
    data = json.loads(result)
    assert "error" in data


async def test_get_top_forecasts_no_connector():
    pool = AsyncMock()
    ctx = _make_ctx(None)
    result = await _execute_tool(pool, "get_top_forecasts", {"asset_class": "equity"}, ctx=ctx)
    data = json.loads(result)
    assert "error" in data


async def test_get_top_forecasts_etf():
    pool = AsyncMock()
    ch = AsyncMock()
    ch.query = AsyncMock(return_value=[{"symbol": "NIFTYBEES", "combined_forecast": 0.4}])
    ctx = _make_ctx(ch)
    result = await _execute_tool(
        pool, "get_top_forecasts", {"asset_class": "etf", "count": 5}, ctx=ctx
    )
    data = json.loads(result)
    assert isinstance(data, list)
    call_args = ch.query.call_args
    sql = call_args[0][0]
    assert "etf_forecasts" in sql


# --- get_trade_decisions ---


async def test_get_trade_decisions_latest():
    pool = AsyncMock()
    ch = AsyncMock()
    # First call: max date query, second call: actual decisions query
    ch.query = AsyncMock(
        side_effect=[
            [{"max_date": "2026-03-28"}],
            [
                {"symbol": "RELIANCE", "conviction": 0.9, "action": "BUY"},
                {"symbol": "INFY", "conviction": -0.7, "action": "SELL"},
            ],
        ]
    )
    ctx = _make_ctx(ch)
    result = await _execute_tool(pool, "get_trade_decisions", {}, ctx=ctx)
    data = json.loads(result)
    assert isinstance(data, list)
    assert len(data) == 2
    assert ch.query.call_count == 2


async def test_get_trade_decisions_specific_date():
    pool = AsyncMock()
    ch = AsyncMock()
    ch.query = AsyncMock(return_value=[{"symbol": "TCS", "conviction": 0.8, "action": "BUY"}])
    ctx = _make_ctx(ch)
    result = await _execute_tool(pool, "get_trade_decisions", {"date": "2026-03-27"}, ctx=ctx)
    data = json.loads(result)
    assert isinstance(data, list)
    assert len(data) == 1
    # Only one query call when date is explicitly provided
    assert ch.query.call_count == 1
    call_args = ch.query.call_args
    params = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("params", {})
    assert params.get("date") == "2026-03-27"


async def test_get_trade_decisions_no_connector():
    pool = AsyncMock()
    ctx = _make_ctx(None)
    result = await _execute_tool(pool, "get_trade_decisions", {}, ctx=ctx)
    data = json.loads(result)
    assert "error" in data


# --- get_instrument_detail ---


async def test_get_instrument_detail_equity():
    pool = AsyncMock()
    ch = AsyncMock()
    ch.query = AsyncMock(
        return_value=[
            {
                "nse_symbol": "RELIANCE",
                "combined_forecast": 0.82,
                "close": 2800.0,
                "sector": "Energy",
            }
        ]
    )
    ctx = _make_ctx(ch)
    result = await _execute_tool(pool, "get_instrument_detail", {"symbol": "RELIANCE"}, ctx=ctx)
    data = json.loads(result)
    assert data["nse_symbol"] == "RELIANCE"
    assert data["combined_forecast"] == 0.82
    assert ch.query.call_count == 1


async def test_get_instrument_detail_crypto_fallback():
    pool = AsyncMock()
    ch = AsyncMock()
    ch.query = AsyncMock(
        side_effect=[
            [],  # equity not found
            [{"symbol": "BTC", "combined_forecast": 0.65, "close": 65000.0}],
        ]
    )
    ctx = _make_ctx(ch)
    result = await _execute_tool(pool, "get_instrument_detail", {"symbol": "BTC"}, ctx=ctx)
    data = json.loads(result)
    assert data["symbol"] == "BTC"
    assert data["combined_forecast"] == 0.65
    assert ch.query.call_count == 2


async def test_get_instrument_detail_not_found():
    pool = AsyncMock()
    ch = AsyncMock()
    ch.query = AsyncMock(side_effect=[[], []])
    ctx = _make_ctx(ch)
    result = await _execute_tool(pool, "get_instrument_detail", {"symbol": "UNKNOWN"}, ctx=ctx)
    data = json.loads(result)
    assert "error" in data
    assert "UNKNOWN" in data["error"]


async def test_get_instrument_detail_missing_symbol():
    pool = AsyncMock()
    ch = AsyncMock()
    ctx = _make_ctx(ch)
    result = await _execute_tool(pool, "get_instrument_detail", {}, ctx=ctx)
    data = json.loads(result)
    assert "error" in data


# --- get_sector_overview ---


async def test_get_sector_overview():
    pool = AsyncMock()
    ch = AsyncMock()
    ch.query = AsyncMock(
        return_value=[
            {"sector": "Technology", "date": "2026-03-29", "avg_forecast": 0.72, "momentum": 0.85},
            {"sector": "Energy", "date": "2026-03-29", "avg_forecast": 0.55, "momentum": 0.60},
        ]
    )
    ctx = _make_ctx(ch)
    result = await _execute_tool(pool, "get_sector_overview", {}, ctx=ctx)
    data = json.loads(result)
    assert isinstance(data, list)
    assert len(data) == 2
    assert data[0]["sector"] == "Technology"
    assert data[1]["sector"] == "Energy"
    ch.query.assert_called_once()


# --- get_forecast_changes ---


async def test_get_forecast_changes_equity():
    pool = AsyncMock()
    ch = AsyncMock()
    ch.query = AsyncMock(
        return_value=[
            {
                "symbol": "RELIANCE",
                "current_forecast": 12.0,
                "prior_forecast": 3.0,
                "change": 9.0,
                "current_date": "2026-03-28",
                "prior_date": "2026-03-23",
            },
        ]
    )
    ctx = _make_ctx(ch)
    result = await _execute_tool(pool, "get_forecast_changes", {"asset_class": "equity"}, ctx=ctx)
    data = json.loads(result)
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["symbol"] == "RELIANCE"
    assert data[0]["change"] == 9.0
    ch.query.assert_called_once()


async def test_get_forecast_changes_missing_asset_class():
    pool = AsyncMock()
    ch = AsyncMock()
    ctx = _make_ctx(ch)
    result = await _execute_tool(pool, "get_forecast_changes", {}, ctx=ctx)
    data = json.loads(result)
    assert "error" in data
