"""Tests for the market summary endpoint (FinanceConnector-backed)."""

from unittest.mock import AsyncMock

import pytest
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.config import Settings
from fastapi.testclient import TestClient

QUOTES = [
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
]


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
    )


@pytest.fixture
def app(settings):
    app = create_app(run_lifespan=False)
    app.dependency_overrides[get_settings] = lambda: settings

    mock_fin = AsyncMock()
    mock_fin.get_overview = AsyncMock(return_value=QUOTES)
    app.state.finance_connector = mock_fin
    app.state.db_pool = AsyncMock()
    app.state.settings = settings
    return app


@pytest.fixture
def client(app):
    return TestClient(app, headers={"X-API-Key": "test-key"})


def test_market_summary_returns_indices(client):
    resp = client.get("/api/market/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is True
    assert data["indices"] == QUOTES


def test_market_summary_drops_errored_quotes(client, app):
    app.state.finance_connector.get_overview = AsyncMock(
        return_value=[QUOTES[0], {"symbol": "^BAD", "error": "timeout"}]
    )
    data = client.get("/api/market/summary").json()
    assert data["available"] is True
    assert data["indices"] == [QUOTES[0]]


def test_market_summary_no_connector(client, app):
    app.state.finance_connector = None
    data = client.get("/api/market/summary").json()
    assert data["available"] is False


def test_market_summary_all_errors(client, app):
    app.state.finance_connector.get_overview = AsyncMock(
        return_value=[{"symbol": "^GSPC", "error": "HTTP 503"}]
    )
    data = client.get("/api/market/summary").json()
    assert data["available"] is False


def test_market_summary_connector_exception(client, app):
    app.state.finance_connector.get_overview = AsyncMock(
        side_effect=Exception("provider down")
    )
    data = client.get("/api/market/summary").json()
    assert data["available"] is False
