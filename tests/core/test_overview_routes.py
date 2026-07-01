"""Tests for overview + system info + settings index routes."""

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
    )


@pytest.fixture
def app(settings):
    app = create_app(run_lifespan=False)
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.db_pool = AsyncMock()
    app.state.settings = settings
    return app


@pytest.fixture
def client(app):
    return TestClient(app, headers={"X-API-Key": "test-key"})


@pytest.fixture
def unauth_client(app):
    return TestClient(app)


def test_brief_requires_auth(unauth_client):
    assert unauth_client.get("/api/overview/brief").status_code == 401


def test_brief_returns_counts(client, app):
    pool = app.state.db_pool
    pool.fetchval = AsyncMock(side_effect=[1, 2])
    resp = client.get("/api/overview/brief")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "pending_interactions": 1,
        "recent_alerts_24h": 2,
    }


def test_status_returns_last_workflow_runs(client, app):
    pool = app.state.db_pool
    pool.fetch = AsyncMock(
        return_value=[
            {"workflow_type": "DailyBriefingFlow", "last_run": "2026-04-15T10:00:00Z"},
        ]
    )
    resp = client.get("/api/overview/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "last_workflow_runs" in body
    assert len(body["last_workflow_runs"]) == 1
    assert body["last_workflow_runs"][0]["workflow_type"] == "DailyBriefingFlow"


def test_system_info(client):
    resp = client.get("/api/system/info")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == "2.0.0"
    assert "git_sha" in body
    assert isinstance(body["uptime_seconds"], int)


def test_list_settings_requires_auth(unauth_client):
    assert unauth_client.get("/api/settings").status_code == 401


def test_list_settings_returns_rows(client, app):
    pool = app.state.db_pool
    pool.fetch = AsyncMock(
        return_value=[
            {"key": "email.junk_domains", "value": ["foo.com"], "updated_at": "2026-04-01"},
        ]
    )
    resp = client.get("/api/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert body[0]["key"] == "email.junk_domains"
