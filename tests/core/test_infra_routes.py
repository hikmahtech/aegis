"""Tests for /api/infra/* routes."""

import json
from unittest.mock import AsyncMock, patch

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
    app.state.remote_script_connector = AsyncMock()
    return app


@pytest.fixture
def client(app):
    return TestClient(app, headers={"X-API-Key": "test-key"})


@pytest.fixture
def unauth_client(app):
    return TestClient(app)


def test_list_services_requires_auth(unauth_client):
    assert unauth_client.get("/api/infra/services").status_code == 401


def test_list_services_delegates(client):
    with patch(
        "aegis.services.chat._exec_list_services",
        new=AsyncMock(return_value=json.dumps([{"name": "aegis_core"}])),
    ):
        resp = client.get("/api/infra/services")
    assert resp.status_code == 200
    assert resp.json() == [{"name": "aegis_core"}]


def test_restart_service_delegates(client):
    with patch(
        "aegis.services.chat._exec_restart_service",
        new=AsyncMock(return_value=json.dumps({"status": "ok"})),
    ):
        resp = client.post("/api/infra/services/aegis_core/restart")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_pod_logs_delegates(client):
    with patch(
        "aegis.services.chat._exec_get_pod_logs",
        new=AsyncMock(return_value=json.dumps({"lines": ["hello"]})),
    ):
        resp = client.get("/api/infra/pods/default/mypod/logs?tail=50")
    assert resp.status_code == 200
    assert resp.json()["lines"] == ["hello"]


def test_argocd_sync_delegates(client):
    with patch(
        "aegis.services.chat.TOOL_EXECUTORS",
        {"sync_argocd_app": AsyncMock(return_value=json.dumps({"synced": True}))},
    ):
        resp = client.post("/api/infra/argocd/apps/myapp/sync")
    assert resp.status_code == 200
    assert resp.json()["synced"] is True


def test_non_json_output_wrapped(client):
    with patch(
        "aegis.services.chat._exec_list_services",
        new=AsyncMock(return_value="plain text output"),
    ):
        resp = client.get("/api/infra/services")
    assert resp.status_code == 200
    assert resp.json() == {"output": "plain text output"}
