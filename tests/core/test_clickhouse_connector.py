"""Tests for ClickHouseConnector."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from aegis.connectors.clickhouse import ClickHouseConnector


@pytest.fixture
def connector():
    return ClickHouseConnector(
        host="localhost",
        port=8123,
        user="test_user",
        password="test_pass",
        database="test_db",
    )


async def test_query_returns_parsed_rows(connector):
    """query() should parse JSONEachRow response into list of dicts."""
    response_text = '{"symbol":"INFY","close":1500.5}\n{"symbol":"TCS","close":3200.0}\n'
    mock_response = httpx.Response(200, text=response_text)

    with patch.object(connector, "_client", create=True) as mock_client:
        mock_client.is_closed = False
        mock_client.get = AsyncMock(return_value=mock_response)
        connector._client = mock_client

        rows = await connector.query("SELECT symbol, close FROM eod_prices FINAL LIMIT 2")

    assert len(rows) == 2
    assert rows[0]["symbol"] == "INFY"
    assert rows[1]["close"] == 3200.0


async def test_query_with_params(connector):
    """query() should pass params as query parameters."""
    mock_response = httpx.Response(200, text='{"symbol":"INFY"}\n')

    with patch.object(connector, "_client", create=True) as mock_client:
        mock_client.is_closed = False
        mock_client.get = AsyncMock(return_value=mock_response)
        connector._client = mock_client

        await connector.query(
            "SELECT * FROM eod_prices WHERE nse_symbol = {sym:String}",
            params={"sym": "INFY"},
        )

    call_kwargs = mock_client.get.call_args
    assert "param_sym" in call_kwargs.kwargs.get("params", {})


async def test_query_empty_response(connector):
    """query() should return empty list for empty response."""
    mock_response = httpx.Response(200, text="")

    with patch.object(connector, "_client", create=True) as mock_client:
        mock_client.is_closed = False
        mock_client.get = AsyncMock(return_value=mock_response)
        connector._client = mock_client

        rows = await connector.query("SELECT 1 WHERE 1=0")

    assert rows == []


async def test_query_http_error_raises(connector):
    """query() should raise on HTTP errors."""
    mock_response = httpx.Response(500, text="DB error")

    with patch.object(connector, "_client", create=True) as mock_client:
        mock_client.is_closed = False
        mock_client.get = AsyncMock(return_value=mock_response)
        connector._client = mock_client

        with pytest.raises(httpx.HTTPStatusError):
            await connector.query("SELECT bad_query")


async def test_health_success(connector):
    """health() should return True when ClickHouse responds with 200."""
    mock_response = httpx.Response(200, text="Ok.\n")

    with patch.object(connector, "_client", create=True) as mock_client:
        mock_client.is_closed = False
        mock_client.get = AsyncMock(return_value=mock_response)
        connector._client = mock_client

        assert await connector.health() is True


async def test_health_failure(connector):
    """health() should return False when ClickHouse is unreachable."""
    with patch.object(connector, "_client", create=True) as mock_client:
        mock_client.is_closed = False
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        connector._client = mock_client

        assert await connector.health() is False


async def test_observability_recording(connector):
    """query() should record to connector_calls when db_pool is set."""
    connector._db_pool = AsyncMock()
    mock_response = httpx.Response(200, text='{"x":1}\n')

    with patch.object(connector, "_client", create=True) as mock_client:
        mock_client.is_closed = False
        mock_client.get = AsyncMock(return_value=mock_response)
        connector._client = mock_client

        # _record now lives in HTTPConnector, which imports the helper lazily
        # from aegis.observability — patch it at its definition site.
        with patch("aegis.observability.record_connector_call") as mock_record:
            mock_record.return_value = None
            await connector.query("SELECT 1")
            mock_record.assert_called_once()
            call_kwargs = mock_record.call_args
            assert call_kwargs.kwargs["connector"] == "clickhouse"
