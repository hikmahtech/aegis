"""Tests for AEGIS v2 database module."""

from unittest.mock import AsyncMock, patch

from aegis.db import check_health, create_pool


async def test_create_pool_returns_pool():
    """create_pool returns an asyncpg pool."""
    with patch("aegis.db.pool.asyncpg.create_pool", new_callable=AsyncMock) as mock:
        mock.return_value = AsyncMock()
        pool = await create_pool("postgresql://test:test@localhost:5432/test")
        assert pool is not None
        mock.assert_called_once()


async def test_check_health_ok():
    """check_health returns ok when DB is reachable."""
    mock_pool = AsyncMock()
    mock_pool.fetchval.return_value = 1
    result = await check_health(mock_pool)
    assert result["status"] == "ok"
    assert "latency_ms" in result


async def test_check_health_error():
    """check_health returns error on failure."""
    mock_pool = AsyncMock()
    mock_pool.fetchval.side_effect = Exception("connection refused")
    result = await check_health(mock_pool)
    assert result["status"] == "error"
