"""Tests for market data briefing activity and formatting."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from aegis_worker.activities.briefing import BriefingActivities
from temporalio.testing import ActivityEnvironment

MOCK_SUMMARY = {
    "available": True,
    "equity_regime": {"regime": "BULL_TREND", "close": 22500, "date": "2026-03-28"},
    "crypto": {
        "fear_greed": 13,
        "fear_greed_label": "Extreme Fear",
        "market_cap_usd": 2.06e12,
        "date": "2026-03-28",
    },
    "top_equity_signals": [
        {
            "symbol": "AUROPHARMA",
            "combined_forecast": 7.5,
            "confidence": 0.8,
            "close_price": 1200,
            "is_halal": 1,
        },
        {
            "symbol": "HDFCBANK",
            "combined_forecast": -5.2,
            "confidence": 0.7,
            "close_price": 1600,
            "is_halal": 1,
        },
    ],
    "top_crypto_signals": [
        {
            "symbol": "BTCUSDT",
            "combined_forecast": 5.0,
            "confidence": 0.7,
            "close_price": 65000,
        },
    ],
    "notable_changes": [
        {"symbol": "TCS", "current_forecast": 8.0, "prior_forecast": 2.0, "change": 6.0},
    ],
    "trade_decisions": [
        {
            "symbol": "CTKUSDT",
            "direction": "LONG",
            "target_weight": 0.1,
            "combined_forecast": 2.3,
            "asset_class": "crypto",
        },
    ],
    "sectors": [
        {"sector": "IT", "momentum_signal": "BULLISH", "sector_rank": 1, "mean_z_score_20d": 0.5},
    ],
}


@pytest.mark.asyncio
async def test_gather_market_data_ok():
    env = ActivityEnvironment()
    mock_response = httpx.Response(
        200,
        json=MOCK_SUMMARY,
        request=httpx.Request("GET", "http://core:8080/api/market/summary"),
    )

    act = BriefingActivities(
        db_pool=None,
        llm_client=None,
        knowledge_connector=None,
        core_api_url="http://core:8080",
        api_key="test-key",
    )
    with patch("aegis_worker.activities.briefing.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        result = await env.run(act.gather_market_data)

    assert result["available"] is True
    assert result["equity_regime"]["regime"] == "BULL_TREND"


@pytest.mark.asyncio
async def test_gather_market_data_no_url():
    env = ActivityEnvironment()
    act = BriefingActivities(
        db_pool=None,
        llm_client=None,
        knowledge_connector=None,
        core_api_url="",
        api_key="",
    )
    result = await env.run(act.gather_market_data)
    assert result["available"] is False


@pytest.mark.asyncio
async def test_gather_market_data_api_error():
    env = ActivityEnvironment()
    act = BriefingActivities(
        db_pool=None,
        llm_client=None,
        knowledge_connector=None,
        core_api_url="http://core:8080",
        api_key="test-key",
    )
    with patch("aegis_worker.activities.briefing.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=Exception("connection refused"))
        mock_cls.return_value = mock_client

        result = await env.run(act.gather_market_data)

    assert result["available"] is False


@pytest.mark.asyncio
async def test_format_market_section():
    env = ActivityEnvironment()
    act = BriefingActivities(db_pool=None, llm_client=None, knowledge_connector=None)
    result = await env.run(act.format_market_section, MOCK_SUMMARY)
    assert "<b>Market Intelligence</b>" in result
    assert "BULL_TREND" in result
    assert "Extreme Fear" in result
    assert "AUROPHARMA" in result
    assert "BTCUSDT" in result
    assert "TCS" in result
    assert "CTKUSDT" in result
    assert "IT" in result


@pytest.mark.asyncio
async def test_format_market_section_unavailable():
    env = ActivityEnvironment()
    act = BriefingActivities(db_pool=None, llm_client=None, knowledge_connector=None)
    result = await env.run(act.format_market_section, {"available": False})
    assert result == ""
