"""Tests for temporal proxy endpoints."""

import base64

import pytest
import respx
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from httpx import ASGITransport, AsyncClient, Response


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


@respx.mock
async def test_list_workflows(app, auth_headers):
    respx.get("http://localhost:8233/api/v1/namespaces/default/workflows").mock(
        return_value=Response(
            200,
            json={
                "executions": [
                    {
                        "execution": {
                            "workflowId": "wf-1",
                            "runId": "run-1",
                        },
                        "type": {"name": "EmailTriageWorkflow"},
                        "status": 2,
                        "startTime": "2026-03-16T10:00:00Z",
                    }
                ]
            },
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/temporal/workflows", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["executions"]) == 1
        assert data["executions"][0]["type"]["name"] == "EmailTriageWorkflow"


@respx.mock
async def test_list_workflows_temporal_down(app, auth_headers):
    respx.get("http://localhost:8233/api/v1/namespaces/default/workflows").mock(
        side_effect=Exception("Connection refused")
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/temporal/workflows", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["executions"] == []
        assert "error" in data


@respx.mock
async def test_workflow_detail(app, auth_headers):
    root = "http://localhost:8233/api/v1/namespaces/default/workflows/wf-1"
    respx.get(root).mock(
        return_value=Response(
            200,
            json={
                "workflowExecutionInfo": {
                    "type": {"name": "ClarifyFlow"},
                    "status": "WORKFLOW_EXECUTION_STATUS_COMPLETED",
                }
            },
        )
    )
    respx.get(f"{root}/history").mock(
        return_value=Response(
            200,
            json={
                "history": {
                    "events": [
                        {"eventId": "1", "eventType": "EVENT_TYPE_WORKFLOW_EXECUTION_STARTED"}
                    ]
                }
            },
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/temporal/workflows/wf-1", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["describe"]["workflowExecutionInfo"]["type"]["name"] == "ClarifyFlow"
    assert data["history"]["history"]["events"][0]["eventId"] == "1"
    assert "error" not in data


@respx.mock
async def test_workflow_detail_temporal_down(app, auth_headers):
    respx.route(
        method="GET",
        url__regex=r"http://localhost:8233/api/v1/namespaces/default/workflows/wf-1.*",
    ).mock(side_effect=Exception("Connection refused"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/temporal/workflows/wf-1", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["describe"] is None
    assert data["history"] is None
    assert "error" in data


async def test_temporal_config(app, auth_headers):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/temporal/config", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "temporal_ui_url" in data
        assert data["temporal_ui_url"] == "https://temporal.example.com"


async def test_temporal_config_includes_knowledge(app, auth_headers):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/temporal/config", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "n8n_ui_url" not in data
    assert "knowledge_ui_url" in data
    # knowledge_ui_url defaults to "" when not configured
    assert isinstance(data["knowledge_ui_url"], str)
