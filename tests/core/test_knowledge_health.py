"""Tests for GET /api/knowledge/health endpoint."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock

import pytest
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def mock_knowledge():
    kc = AsyncMock()
    kc.get_stats.return_value = {"triples": 1500, "entities": 300, "content": 42}
    kc.contradictions.return_value = [
        {"subject": "a", "predicate": "is", "object": "b"},
        {"subject": "c", "predicate": "is", "object": "d"},
    ]
    return kc


@pytest.fixture
def mock_db_pool_health():
    """DB pool with realistic health values."""
    pool = AsyncMock()

    async def fetchval_side_effect(query, *args):
        if "pending_claims" in query:
            return 5
        if "knowledge_injection_log" in query:
            return 120
        if "task_rules" in query:
            return 8
        if "task_rule_applications" in query:
            return 200
        return 0

    pool.fetchval.side_effect = fetchval_side_effect
    pool.fetchrow.return_value = None
    pool.execute.return_value = "OK"
    pool.close = AsyncMock()
    return pool


@pytest.fixture
def app_with_health(test_settings, mock_db_pool_health, mock_knowledge):
    application = create_app(run_lifespan=False)
    application.dependency_overrides[get_settings] = lambda: test_settings
    application.state.db_pool = mock_db_pool_health
    application.state.knowledge_connector = mock_knowledge
    return application


@pytest.fixture
def app_no_connector(test_settings, mock_db_pool_health):
    """App without knowledge connector wired."""
    application = create_app(run_lifespan=False)
    application.dependency_overrides[get_settings] = lambda: test_settings
    application.state.db_pool = mock_db_pool_health
    return application


@pytest.fixture
def auth_headers():
    creds = base64.b64encode(b"admin:admin").decode()
    return {"Authorization": f"Basic {creds}"}


async def test_health_endpoint_returns_all_keys(app_with_health, auth_headers):
    """Health endpoint returns all expected top-level keys."""
    transport = ASGITransport(app=app_with_health)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/knowledge/health", headers=auth_headers)

    assert resp.status_code == 200
    data = resp.json()
    expected_keys = {
        "kg_stats",
        "injection_log_30d",
    }
    assert expected_keys == set(data.keys())


async def test_health_kg_stats(app_with_health, auth_headers, mock_knowledge):
    """KG stats from connector are returned verbatim."""
    transport = ASGITransport(app=app_with_health)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/knowledge/health", headers=auth_headers)

    data = resp.json()
    assert data["kg_stats"]["triples"] == 1500
    assert data["kg_stats"]["entities"] == 300
    assert data["kg_stats"]["content"] == 42
    mock_knowledge.get_stats.assert_called_once()


async def test_health_injection_log(app_with_health, auth_headers):
    """Injection log count is returned."""
    transport = ASGITransport(app=app_with_health)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/knowledge/health", headers=auth_headers)

    data = resp.json()
    assert data["injection_log_30d"] == 120


async def test_health_without_connector(app_no_connector, auth_headers):
    """Health endpoint works even when no knowledge connector is wired."""
    transport = ASGITransport(app=app_no_connector)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/knowledge/health", headers=auth_headers)

    assert resp.status_code == 200
    data = resp.json()
    assert data["kg_stats"] == {}


async def test_health_connector_error_graceful(test_settings, mock_db_pool_health, auth_headers):
    """Connector failures are caught and reported gracefully."""
    kc = AsyncMock()
    kc.get_stats.side_effect = Exception("connection refused")
    kc.contradictions.side_effect = Exception("connection refused")

    application = create_app(run_lifespan=False)
    application.dependency_overrides[get_settings] = lambda: test_settings
    application.state.db_pool = mock_db_pool_health
    application.state.knowledge_connector = kc

    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/knowledge/health", headers=auth_headers)

    assert resp.status_code == 200
    data = resp.json()
    assert data["kg_stats"] == {"error": "unavailable"}


async def test_health_requires_auth(app_with_health):
    """Endpoint requires authentication."""
    transport = ASGITransport(app=app_with_health)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/knowledge/health")

    assert resp.status_code == 401
