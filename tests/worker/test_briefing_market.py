"""Tests for market data briefing activity and formatting."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from aegis_worker.activities.briefing import BriefingActivities
from temporalio.testing import ActivityEnvironment

MOCK_SUMMARY = {
    "available": True,
    "indices": [
        {
            "symbol": "^GSPC",
            "price": 5500.25,
            "change": 12.5,
            "change_percent": 0.23,
            "currency": "USD",
            "as_of": "2026-07-02T20:00:00+00:00",
        },
        {
            "symbol": "^NSEI",
            "price": 24100.0,
            "change": -80.0,
            "change_percent": -0.33,
            "currency": "INR",
            "as_of": "2026-07-02T10:00:00+00:00",
        },
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
    assert result["indices"][0]["symbol"] == "^GSPC"


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
    assert "<b>Markets</b>" in result
    assert "^GSPC" in result
    assert "5,500.25" in result
    assert "(+0.23%)" in result
    assert "^NSEI" in result
    assert "(-0.33%)" in result


@pytest.mark.asyncio
async def test_format_market_section_unavailable():
    env = ActivityEnvironment()
    act = BriefingActivities(db_pool=None, llm_client=None, knowledge_connector=None)
    result = await env.run(act.format_market_section, {"available": False})
    assert result == ""


@pytest.mark.asyncio
async def test_format_market_section_no_usable_quotes():
    env = ActivityEnvironment()
    act = BriefingActivities(db_pool=None, llm_client=None, knowledge_connector=None)
    result = await env.run(
        act.format_market_section,
        {"available": True, "indices": [{"symbol": "^GSPC", "error": "timeout"}]},
    )
    assert result == ""
