"""Tests for /api/admin/homelab/* routes."""

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
        admin_username="admin",
        admin_password="admin",
        api_key="test-key",
        homelab_dagster_graphql_url="http://dagster.test/graphql",
        homelab_traefik_api_url="http://traefik.test/api",
        homelab_public_domains=["example.com", "api.example.com"],
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


def test_homelab_state_returns_latest_rows(app, client):
    """GET /state calls all four tables and returns them in the response body."""
    # The conn mock is the one injected via _make_pool; get it from state.
    # We re-create a fresh pool with a conn that returns our test data.
    conn = AsyncMock()
    conn.fetch = AsyncMock(
        side_effect=[
            # drift
            [
                {
                    "id": 1,
                    "service_name": "aegis_core",
                    "stack_name": "aegis",
                    "drift_type": "replicas",
                    "severity": "critical",
                    "detected_at": "2026-04-16T00:00:00Z",
                    "resolved_at": None,
                    "actual": "{}",
                }
            ],
            # backups
            [],
            # schedules
            [],
            # certs
            [
                {
                    "domain": "example.com",
                    "cert_serial": "S1",
                    "not_after": "2026-05-16T00:00:00Z",
                    "days_until_expiry": 30,
                    "last_alert_threshold": None,
                    "checked_at": "2026-04-16T00:00:00Z",
                }
            ],
        ]
    )
    app.state.db_pool = _make_pool(conn)

    resp = client.get("/api/admin/homelab/state")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["drift"]) == 1
    assert body["drift"][0]["service_name"] == "aegis_core"
    assert len(body["certs"]) == 1
    assert body["certs"][0]["domain"] == "example.com"
    assert "backups" in body
    assert "schedules" in body


def test_homelab_trigger_run_queues_workflow(app, client, monkeypatch):
    """POST /{flow}/run delegates to _start_workflow and returns the workflow id."""
    calls = []

    async def fake_start(flow, cfg, temporal_client):
        calls.append((flow, cfg))
        handle = MagicMock()
        handle.id = "wf-1"
        return handle

    monkeypatch.setattr("aegis.api.routes.homelab._start_workflow", fake_start)

    resp = client.post("/api/admin/homelab/service_drift/run")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["workflow_id"] == "wf-1"
    assert len(calls) == 1
    assert calls[0][0] == "service_drift"


def test_cert_radar_trigger_uses_settings_domains(app, client, monkeypatch):
    """Manual cert_radar trigger with no body pulls domains from settings."""
    calls = []

    async def fake_start(flow, cfg, temporal_client):
        calls.append((flow, cfg))
        handle = MagicMock()
        handle.id = "wf-cr-1"
        return handle

    monkeypatch.setattr("aegis.api.routes.homelab._start_workflow", fake_start)

    resp = client.post("/api/admin/homelab/cert_radar/run")

    assert resp.status_code == 200
    assert calls[0][0] == "cert_radar"
    assert calls[0][1]["domains"] == ["example.com", "api.example.com"]


def test_cert_radar_trigger_body_overrides_defaults(app, client, monkeypatch):
    """Explicit body domains override the settings fallback."""
    calls = []

    async def fake_start(flow, cfg, temporal_client):
        calls.append((flow, cfg))
        handle = MagicMock()
        handle.id = "wf-cr-2"
        return handle

    monkeypatch.setattr("aegis.api.routes.homelab._start_workflow", fake_start)

    resp = client.post(
        "/api/admin/homelab/cert_radar/run",
        json={"domains": ["override.test"]},
    )

    assert resp.status_code == 200
    assert calls[0][1]["domains"] == ["override.test"]
