"""Tests for audit log endpoints."""

import base64

import pytest
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def app(test_settings, mock_db_pool):
    application = create_app(run_lifespan=False)
    application.dependency_overrides[get_settings] = lambda: test_settings
    application.state.db_pool = mock_db_pool
    return application


@pytest.fixture
def auth_headers():
    creds = base64.b64encode(b"admin:admin").decode()
    return {"Authorization": f"Basic {creds}"}


async def test_list_audit_log(app, auth_headers, mock_db_pool):
    mock_db_pool.fetch.return_value = [
        {
            "id": "uuid-1",
            "actor": "sebas",
            "action": "task_created",
            "target_type": "task",
            "target_id": "task-1",
            "details": '{"title":"New task"}',
            "created_at": "2026-03-16",
        },
        {
            "id": "uuid-2",
            "actor": "system",
            "action": "alert_received",
            "target_type": "alert",
            "target_id": "alert-1",
            "details": "{}",
            "created_at": "2026-03-16",
        },
    ]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/audit", headers=auth_headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 2


async def test_list_audit_log_with_filters(app, auth_headers, mock_db_pool):
    mock_db_pool.fetch.return_value = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/audit?actor=sebas&target_type=task", headers=auth_headers)
        assert resp.status_code == 200
        call_args = mock_db_pool.fetch.call_args
        query = call_args[0][0]
        assert "actor = $1" in query
        assert "target_type = $2" in query


async def test_list_audit_log_with_limit(app, auth_headers, mock_db_pool):
    mock_db_pool.fetch.return_value = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/audit?limit=10", headers=auth_headers)
        assert resp.status_code == 200
        call_args = mock_db_pool.fetch.call_args
        assert 10 in call_args[0][1:]
