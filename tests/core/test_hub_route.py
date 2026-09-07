"""`POST /api/hub/events`: token gate, body shape, and the ingest result."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.config import Settings
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.asyncio

_SETTINGS = {
    "database_url": "postgresql://test:test@localhost:5432/test",
    "litellm_url": "https://litellm.example.com/v1",
    "temporal_ui_url": "https://temporal.example.com",
    "n8n_ui_url": "https://n8n.example.com",
    "admin_username": "admin",
    "admin_password": "admin",
    "n8n_webhook_secret": "test-secret",
}


def _client(pool, secret: str = ""):
    app = create_app(run_lifespan=False)
    app.state.db_pool = pool
    app.dependency_overrides[get_settings] = lambda: Settings(**_SETTINGS, alert_webhook_secret=secret)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest_asyncio.fixture(loop_scope="function")
async def hub_client(db_pool):
    async with _client(db_pool, secret="s3cret") as c:
        yield c


def _body(**kw) -> dict:
    return {
        "source": "ansible",
        "external_id": f"deploy-{uuid.uuid4()}",
        "kind": "occurrence",
        "title": "deploy failed",
        "class": "deploy_failed",
        "subject": f"svc_{uuid.uuid4().hex[:8]}",
        **kw,
    }


async def test_rejects_without_token(hub_client):
    r = await hub_client.post("/api/hub/events", json=_body())
    assert r.status_code == 401


async def test_accepts_x_alert_token_and_bearer(hub_client):
    r = await hub_client.post("/api/hub/events", json=_body(), headers={"X-Alert-Token": "s3cret"})
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "created"
    r = await hub_client.post(
        "/api/hub/events", json=_body(), headers={"Authorization": "bearer s3cret"}
    )
    assert r.status_code == 200 and r.json()["action"] == "created"


async def test_blank_secret_leaves_the_route_open(db_pool):
    async with _client(db_pool) as c:
        r = await c.post("/api/hub/events", json=_body())
    assert r.status_code == 200 and r.json()["problem_id"]


async def test_bad_source_is_a_400_not_a_problem(hub_client):
    r = await hub_client.post(
        "/api/hub/events", json=_body(source="telegram"), headers={"X-Alert-Token": "s3cret"}
    )
    assert r.status_code == 400
    assert "unknown source" in r.json()["detail"]


async def test_second_post_attaches_and_returns_the_same_problem(hub_client):
    body = _body()
    h = {"X-Alert-Token": "s3cret"}
    first = (await hub_client.post("/api/hub/events", json=body, headers=h)).json()
    second = (
        await hub_client.post(
            "/api/hub/events", json={**body, "external_id": f"x-{uuid.uuid4()}"}, headers=h
        )
    ).json()
    assert second["problem_id"] == first["problem_id"]
    assert second["action"] == "attached" and second["occurrences"] == 2
    assert second["key"] == f"deploy_failed:service:{body['subject']}"


async def test_missing_pool_is_a_503(db_pool):
    async with _client(None) as c:
        r = await c.post("/api/hub/events", json=_body())
    assert r.status_code == 503
