"""Tests for POST /api/admin/capture (chat /capture backend)."""

from __future__ import annotations

import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.auth import verify_auth
from aegis.api.deps import get_settings
from aegis.config import Settings
from fastapi.testclient import TestClient

_TEST_REQUIRED_SETTINGS = {
    "database_url": "postgresql://test:test@localhost:5432/test",
    "litellm_url": "https://litellm.example.com/v1",
    "temporal_ui_url": "https://temporal.example.com",
    "n8n_ui_url": "https://n8n.example.com",
    "admin_username": "admin",
    "admin_password": "admin",
}


@pytest.fixture
def settings():
    return Settings(**_TEST_REQUIRED_SETTINGS)


@pytest_asyncio.fixture(loop_scope="function")
async def app_client(settings, db_pool):
    """FastAPI client with verify_auth + get_settings overridden."""
    app = create_app(run_lifespan=False)
    app.state.db_pool = db_pool
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[verify_auth] = lambda: None
    return TestClient(app)


def test_capture_route_returns_task_ref(app_client, monkeypatch):
    """When _capture_to_inbox_impl returns a ref, the route returns it."""
    async def fake_capture(pool, source_tag, external_id, title, description):
        return "TASK-CAPTURE-1"

    monkeypatch.setattr(
        "aegis.services.chat._capture_to_inbox_impl", fake_capture, raising=False
    )
    r = app_client.post(
        "/api/admin/capture",
        json={"text": "buy milk", "source": "chat"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task_ref"] == "TASK-CAPTURE-1"
    assert body["source_tag"] == "#chat"
    # Default external_id is hash-based
    assert body["external_id"].startswith("chat:")


def test_capture_route_honors_explicit_external_id(app_client, monkeypatch):
    captured: dict = {}

    async def fake_capture(pool, source_tag, external_id, title, description):
        captured["ext_id"] = external_id
        return "TASK-CAPTURE-2"

    monkeypatch.setattr(
        "aegis.services.chat._capture_to_inbox_impl", fake_capture, raising=False
    )
    r = app_client.post(
        "/api/admin/capture",
        json={"text": "x", "source": "chat", "external_id": "chat:12345:abc"},
    )
    assert r.status_code == 200
    assert captured["ext_id"] == "chat:12345:abc"


def test_capture_route_503_when_impl_returns_none(app_client, monkeypatch):
    """Kill-switch off / missing inbox → 503."""
    async def fake_capture(*a, **kw):
        return None

    monkeypatch.setattr(
        "aegis.services.chat._capture_to_inbox_impl", fake_capture, raising=False
    )
    r = app_client.post("/api/admin/capture", json={"text": "x"})
    assert r.status_code == 503
    assert "capture skipped" in r.text


def test_capture_route_rejects_empty_text(app_client):
    r = app_client.post("/api/admin/capture", json={"text": ""})
    assert r.status_code == 422  # pydantic min_length=1


def test_capture_route_rejects_bad_source(app_client):
    r = app_client.post(
        "/api/admin/capture",
        json={"text": "x", "source": "WITH-DASH"},  # uppercase + dash not allowed
    )
    assert r.status_code == 422
