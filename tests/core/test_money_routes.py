"""Tests for /api/admin/money/* routes."""

from unittest.mock import AsyncMock, MagicMock

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
        money_hygiene_enabled=True,
    )


def _make_pool(conn):
    """Build a db_pool mock whose acquire() works as an async context manager."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool


@pytest.fixture
def app(settings):
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)
    pool = _make_pool(conn)
    application = create_app(run_lifespan=False)
    application.dependency_overrides[get_settings] = lambda: settings
    application.state.db_pool = pool
    application.state.settings = settings
    application.state.temporal_client = AsyncMock()
    return application


@pytest.fixture
def client(app):
    return TestClient(app, headers={"X-API-Key": "test-key"})


def test_money_state_returns_charges_and_alerts(app, client):
    """GET /state returns active charges + upcoming renewal alerts."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(
        side_effect=[
            # charges
            [
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "account": "user-personal",
                    "vendor_name": "Namecheap",
                    "category": "domain",
                    "amount_cents": 1299,
                    "currency": "USD",
                    "monthly_home_equivalent": 91.45,
                    "cadence": "yearly",
                    "next_due_at": "2027-04-15T00:00:00Z",
                    "status": "active",
                    "last_seen_at": "2026-04-15T00:00:00Z",
                    "first_seen_at": "2026-04-15T00:00:00Z",
                }
            ],
            # upcoming alerts
            [
                {
                    "charge_id": "11111111-1111-1111-1111-111111111111",
                    "threshold_days": 30,
                    "fired_at": "2026-04-16T00:00:00Z",
                    "vendor_name": "Namecheap",
                    "amount_cents": 1299,
                    "currency": "USD",
                    "next_due_at": "2027-04-15T00:00:00Z",
                }
            ],
        ]
    )
    app.state.db_pool = _make_pool(conn)

    resp = client.get("/api/admin/money/state")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["charges"]) == 1
    assert body["charges"][0]["vendor_name"] == "Namecheap"
    assert len(body["upcoming_alerts"]) == 1
    assert body["upcoming_alerts"][0]["threshold_days"] == 30


def test_money_digest_returns_none_when_empty(app, client):
    """GET /digest returns {digest: None} when no rows exist."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    app.state.db_pool = _make_pool(conn)

    resp = client.get("/api/admin/money/digest")

    assert resp.status_code == 200
    assert resp.json() == {"digest": None}


def test_money_digest_returns_latest_when_present(app, client):
    """GET /digest returns the latest digest row."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "period_start": "2026-03-01",
            "period_end": "2026-03-31",
            "summary": {"total_monthly_inr": 12345.67, "active_count": 5},
            "sent_at": "2026-04-01T00:00:00Z",
        }
    )
    app.state.db_pool = _make_pool(conn)

    resp = client.get("/api/admin/money/digest")

    assert resp.status_code == 200
    body = resp.json()
    assert body["digest"] is not None
    assert body["digest"]["period_start"] == "2026-03-01"
    assert body["digest"]["summary"]["total_monthly_inr"] == 12345.67


@pytest.mark.parametrize("flow", ["money_hygiene", "subscription_audit"])
def test_money_trigger_run_dispatches_workflow(app, client, monkeypatch, flow):
    """POST /{flow}/run delegates to _start_workflow for each known flow."""
    calls = []

    async def fake_start(flow_name, cfg, temporal_client):
        calls.append((flow_name, cfg))
        handle = MagicMock()
        handle.id = f"wf-{flow_name}-1"
        return handle

    monkeypatch.setattr("aegis.api.routes.money._start_workflow", fake_start)

    resp = client.post(f"/api/admin/money/{flow}/run")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["workflow_id"] == f"wf-{flow}-1"
    assert calls[0][0] == flow


def test_money_trigger_run_409_when_disabled(app, client, settings):
    """POST /{flow}/run returns 409 when money_hygiene_enabled is False."""
    settings.money_hygiene_enabled = False
    app.dependency_overrides[get_settings] = lambda: settings

    resp = client.post("/api/admin/money/money_hygiene/run")

    assert resp.status_code == 409
    assert "disabled" in resp.json()["detail"].lower()


def test_money_trigger_run_503_when_no_temporal(app, client):
    """POST /{flow}/run returns 503 when temporal_client is None."""
    app.state.temporal_client = None

    resp = client.post("/api/admin/money/money_hygiene/run")

    assert resp.status_code == 503


def test_money_trigger_run_400_for_unknown_flow(app, client):
    """POST /{unknown}/run returns 400 for unrecognized flow names."""
    resp = client.post("/api/admin/money/bogus_flow/run")

    assert resp.status_code == 400
