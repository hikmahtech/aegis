"""Tests for observability endpoints (LLM calls, connector calls)."""

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


async def test_list_llm_calls(app, auth_headers, mock_db_pool):
    mock_db_pool.fetch.return_value = [
        {
            "id": "uuid-1",
            "model": "gpt-4",
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "latency_ms": 800,
            "purpose": "chat",
            "agent_id": "sebas",
            "created_at": "2026-03-16",
        },
        {
            "id": "uuid-2",
            "model": "haiku",
            "prompt_tokens": 200,
            "completion_tokens": 30,
            "latency_ms": 300,
            "purpose": "triage",
            "agent_id": "raphael",
            "created_at": "2026-03-16",
        },
    ]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/observability/llm-calls", headers=auth_headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 2


async def test_list_llm_calls_with_filters(app, auth_headers, mock_db_pool):
    mock_db_pool.fetch.return_value = [
        {
            "id": "uuid-1",
            "model": "gpt-4",
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "latency_ms": 800,
            "purpose": "chat",
            "agent_id": "sebas",
            "created_at": "2026-03-16",
        },
    ]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            "/api/observability/llm-calls?model=gpt-4&purpose=chat", headers=auth_headers
        )
        assert resp.status_code == 200
        call_args = mock_db_pool.fetch.call_args
        query = call_args[0][0]
        assert "model = $1" in query
        assert "purpose = $2" in query


async def test_llm_stats(app, auth_headers, mock_db_pool):
    mock_db_pool.fetchrow.return_value = {
        "total_calls": 42,
        "total_prompt_tokens": 5000,
        "total_completion_tokens": 2000,
        "avg_latency_ms": 500,
        "max_latency_ms": 1200,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/observability/llm-stats", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_calls"] == 42
        assert data["avg_latency_ms"] == 500


async def test_list_connector_calls(app, auth_headers, mock_db_pool):
    mock_db_pool.fetch.return_value = [
        {
            "id": "uuid-1",
            "connector": "gmail",
            "action": "fetch",
            "status": "ok",
            "latency_ms": 200,
            "error": None,
            "created_at": "2026-03-16",
        },
    ]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/observability/connector-calls", headers=auth_headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 1


async def test_list_connector_calls_with_status_filter(app, auth_headers, mock_db_pool):
    mock_db_pool.fetch.return_value = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            "/api/observability/connector-calls?status=error", headers=auth_headers
        )
        assert resp.status_code == 200
        call_args = mock_db_pool.fetch.call_args
        query = call_args[0][0]
        assert "status = $1" in query


async def test_connector_stats(app, auth_headers, mock_db_pool):
    mock_db_pool.fetchrow.return_value = {
        "total_calls": 100,
        "avg_latency_ms": 350,
        "error_count": 5,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/observability/connector-stats", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_calls"] == 100
        assert data["error_count"] == 5


async def test_list_workflow_runs(app, auth_headers, mock_db_pool):
    mock_db_pool.fetch.return_value = [
        {
            "run_id": "run-1",
            "workflow_id": "wf-1",
            "workflow_type": "GmailIngestFlow",
            "agent_id": "sebas",
            "parent_run_id": None,
            "status": "completed",
            "started_at": "2026-04-20T10:00:00Z",
            "completed_at": "2026-04-20T10:00:05Z",
            "duration_ms": 5000,
            "error": None,
            "input_summary": None,
            "result_summary": None,
        },
        {
            "run_id": "run-2",
            "workflow_id": "wf-2",
            "workflow_type": "SentryPollFlow",
            "agent_id": "pandoras-actor",
            "parent_run_id": None,
            "status": "running",
            "started_at": "2026-04-20T11:00:00Z",
            "completed_at": None,
            "duration_ms": None,
            "error": None,
            "input_summary": None,
            "result_summary": None,
        },
    ]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/observability/workflow-runs", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["workflow_type"] == "GmailIngestFlow"


async def test_list_workflow_runs_with_filters(app, auth_headers, mock_db_pool):
    mock_db_pool.fetch.return_value = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            "/api/observability/workflow-runs?agent_id=sebas&workflow_type=GmailIngestFlow&status=completed&limit=50",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        call_args = mock_db_pool.fetch.call_args
        query = call_args[0][0]
        assert "agent_id = $1" in query
        assert "workflow_type = $2" in query
        assert "status = $3" in query
        assert "ORDER BY started_at DESC" in query
        # Params: agent_id, workflow_type, status, limit, offset
        assert call_args[0][1] == "sebas"
        assert call_args[0][2] == "GmailIngestFlow"
        assert call_args[0][3] == "completed"
        assert call_args[0][4] == 50
        assert call_args[0][5] == 0


async def test_list_connector_calls_with_agent_id_filter(app, auth_headers, mock_db_pool):
    mock_db_pool.fetch.return_value = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            "/api/observability/connector-calls?agent_id=sebas", headers=auth_headers
        )
        assert resp.status_code == 200
        call_args = mock_db_pool.fetch.call_args
        query = call_args[0][0]
        assert "agent_id = $1" in query
        assert call_args[0][1] == "sebas"


async def test_connector_stats_with_agent_id_filter(app, auth_headers, mock_db_pool):
    mock_db_pool.fetchrow.return_value = {
        "total_calls": 10,
        "avg_latency_ms": 200,
        "error_count": 0,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            "/api/observability/connector-stats?agent_id=sebas", headers=auth_headers
        )
        assert resp.status_code == 200
        call_args = mock_db_pool.fetchrow.call_args
        query = call_args[0][0]
        assert "agent_id = $1" in query
