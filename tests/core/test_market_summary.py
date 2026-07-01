"""Tests for market summary endpoint."""

from unittest.mock import AsyncMock

import pytest
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.config import Settings
from fastapi.testclient import TestClient


@pytest.fixture
def settings():
    return Settings(
        database_url="postgresql://test:test@localhost/test",
        litellm_url="https://litellm.test/v1",
        temporal_ui_url="https://temporal.test",
        n8n_ui_url="https://n8n.test",
        admin_username="admin",
        admin_password="admin",
        n8n_webhook_secret="test-secret",
        api_key="test-key",
        clickhouse_host="localhost",
    )


@pytest.fixture
def app(settings):
    app = create_app(run_lifespan=False)
    app.dependency_overrides[get_settings] = lambda: settings

    mock_ch = AsyncMock()
    mock_ch.query = AsyncMock(return_value=[])
    app.state.clickhouse_connector = mock_ch
    app.state.db_pool = AsyncMock()
    app.state.settings = settings
    return app


@pytest.fixture
def client(app):
    return TestClient(app, headers={"X-API-Key": "test-key"})


def test_market_summary_returns_structure(client, app):
    ch = app.state.clickhouse_connector
    ch.query = AsyncMock(
        side_effect=[
            [{"date": "2026-03-28", "close": 22500, "regime_label": "BULL_TREND"}],
            [
                {
                    "date": "2026-03-28",
                    "fgi_value": 45,
                    "fgi_classification": "Fear",
                    "total_market_cap_usd": 2.5e12,
                }
            ],
            [
                {
                    "symbol": "INFY",
                    "combined_forecast": 7.5,
                    "confidence": 0.8,
                    "is_halal": 1,
                    "close_price": 1500,
                }
            ],
            [
                {
                    "symbol": "BTCUSDT",
                    "combined_forecast": 5.0,
                    "confidence": 0.7,
                    "close_price": 65000,
                }
            ],
            [
                {
                    "symbol": "TCS",
                    "current_forecast": 8.0,
                    "prior_forecast": 2.0,
                    "change": 6.0,
                }
            ],
            [{"max_date": "2026-03-28"}],
            [
                {
                    "symbol": "INFY",
                    "direction": "LONG",
                    "target_weight": 0.05,
                    "combined_forecast": 7.5,
                }
            ],
            [
                {
                    "sector": "IT",
                    "momentum_signal": "BULLISH",
                    "sector_rank": 1,
                    "mean_z_score_20d": 0.5,
                }
            ],
        ]
    )
    resp = client.get("/api/market/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is True
    assert "equity_regime" in data
    assert "crypto" in data
    assert "top_equity_signals" in data
    assert "top_crypto_signals" in data
    assert "notable_changes" in data
    assert "trade_decisions" in data
    assert "sectors" in data


def test_market_summary_no_clickhouse(client, app):
    app.state.clickhouse_connector = None
    resp = client.get("/api/market/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is False


def test_market_summary_clickhouse_error(client, app):
    ch = app.state.clickhouse_connector
    ch.query = AsyncMock(side_effect=Exception("connection refused"))
    resp = client.get("/api/market/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is False
