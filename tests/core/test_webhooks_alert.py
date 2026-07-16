"""/api/webhooks/alert endpoint (Alertmanager/Grafana)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.api.routes.interactions import get_workflow_client
from aegis.config import Settings
from httpx import ASGITransport, AsyncClient

_TEST_SETTINGS = {
    "database_url": "postgresql://test:test@localhost:5432/test",
    "litellm_url": "https://litellm.example.com/v1",
    "temporal_ui_url": "https://temporal.example.com",
    "n8n_ui_url": "https://n8n.example.com",
    "admin_username": "admin",
    "admin_password": "admin",
    "n8n_webhook_secret": "test-secret",
}


@pytest.fixture
def settings():
    return Settings(**_TEST_SETTINGS)


@pytest.fixture
def temporal_stub():
    handle = MagicMock()
    handle.id = "wf-alert-1"
    client = MagicMock()
    client.start_workflow = AsyncMock(return_value=handle)
    return client


def _mock_pool(fetchval_return=None):
    """Build a minimal mock asyncpg pool whose acquire() context manager yields
    a connection with configurable fetchval return value."""
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=fetchval_return)

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool, conn


@pytest_asyncio.fixture(loop_scope="function")
async def alert_client(settings, temporal_stub):
    """Mock-pool client; fetchval returns the fingerprint (claim succeeds)."""
    pool, _ = _mock_pool(fetchval_return="am-test-1")
    app = create_app(run_lifespan=False)
    app.state.db_pool = pool
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_workflow_client] = lambda: temporal_stub
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, temporal_stub


async def test_alertmanager_firing_spawns_flow(alert_client):
    c, temporal = alert_client
    payload = {
        "version": "4",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "HighCPU", "instance": "node-a", "severity": "critical"},
                "annotations": {"summary": "High CPU on node-a", "description": "95%"},
                "fingerprint": "am-test-1",
            },
        ],
    }
    resp = await c.post("/api/webhooks/alert", content=json.dumps(payload))
    assert resp.status_code == 200
    assert resp.json() == {"accepted": True, "started": 1, "skipped": 0}
    temporal.start_workflow.assert_awaited_once()
    call = temporal.start_workflow.call_args
    assert call.args[0] == "AlertInvestigationFlow"
    assert call.kwargs["id"] == "alertmanager-am-test-1"
    assert call.kwargs["task_queue"] == "aegis-main"
    alert = call.args[1]
    assert alert["source"] == "alertmanager"
    assert alert["severity"] == "critical"
    assert alert["service"] == "node-a"


async def test_resolved_alert_skipped(alert_client):
    c, temporal = alert_client
    payload = {
        "alerts": [
            {"status": "resolved", "labels": {"alertname": "HighCPU"}, "fingerprint": "am-test-2"},
        ],
    }
    resp = await c.post("/api/webhooks/alert", content=json.dumps(payload))
    assert resp.status_code == 200
    assert resp.json() == {"accepted": True, "started": 0, "skipped": 1}
    temporal.start_workflow.assert_not_awaited()


async def test_duplicate_fingerprint_skipped(settings, temporal_stub):
    """First call claims the row (fetchval returns value); second returns None."""
    # First call: fetchval returns fingerprint string (INSERT succeeded)
    pool_first, conn_first = _mock_pool(fetchval_return="am-dup-1")
    app = create_app(run_lifespan=False)
    app.state.db_pool = pool_first
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_workflow_client] = lambda: temporal_stub

    alert_payload = json.dumps(
        {
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "Mem", "instance": "node-a"},
                    "annotations": {"summary": "memory"},
                    "fingerprint": "am-dup-1",
                }
            ]
        }
    ).encode()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r1 = await c.post("/api/webhooks/alert", content=alert_payload)

    assert r1.json()["started"] == 1

    # Second call: fetchval returns None (ON CONFLICT DO NOTHING → no row returned)
    pool_second, _ = _mock_pool(fetchval_return=None)
    app2 = create_app(run_lifespan=False)
    app2.state.db_pool = pool_second
    app2.dependency_overrides[get_settings] = lambda: settings
    app2.dependency_overrides[get_workflow_client] = lambda: temporal_stub

    async with AsyncClient(transport=ASGITransport(app=app2), base_url="http://test") as c2:
        r2 = await c2.post("/api/webhooks/alert", content=alert_payload)

    assert r2.json()["started"] == 0
    assert r2.json()["skipped"] == 1
    assert temporal_stub.start_workflow.await_count == 1


async def test_missing_fingerprint_synthesizes(alert_client):
    c, temporal = alert_client
    payload = {
        "alerts": [{"status": "firing", "labels": {"alertname": "X", "instance": "y"}}],
    }
    resp = await c.post("/api/webhooks/alert", content=json.dumps(payload))
    assert resp.json()["started"] == 1
    call = temporal.start_workflow.call_args
    assert "alertmanager:X:y" in call.kwargs["id"]


async def test_bad_json_returns_400(alert_client):
    c, _ = alert_client
    resp = await c.post("/api/webhooks/alert", content=b"not-json")
    assert resp.status_code == 400


async def test_multiple_alerts_mixed_status(settings, temporal_stub):
    """Two firing + one resolved: started=2, skipped=1."""
    # fetchval returns non-None twice for the two firing alerts
    conn = AsyncMock()
    conn.fetchval = AsyncMock(side_effect=["m1", "m2"])

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire

    app = create_app(run_lifespan=False)
    app.state.db_pool = pool
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_workflow_client] = lambda: temporal_stub

    payload = {
        "alerts": [
            {"status": "firing", "labels": {"alertname": "A"}, "fingerprint": "m1"},
            {"status": "firing", "labels": {"alertname": "B"}, "fingerprint": "m2"},
            {"status": "resolved", "labels": {"alertname": "C"}, "fingerprint": "m3"},
        ],
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/webhooks/alert", content=json.dumps(payload))

    assert resp.json() == {"accepted": True, "started": 2, "skipped": 1}


async def test_bare_single_alert_dict_handled(alert_client):
    """Some ad-hoc integrations POST a single alert dict, not wrapped in `alerts`."""
    c, temporal = alert_client
    payload = {
        "status": "firing",
        "labels": {"alertname": "Z", "severity": "warning"},
        "annotations": {"summary": "test"},
        "fingerprint": "single-1",
    }
    resp = await c.post("/api/webhooks/alert", content=json.dumps(payload))
    assert resp.json()["started"] == 1
    call = temporal.start_workflow.call_args
    assert call.kwargs["id"] == "alertmanager-single-1"


# ---------------------------------------------------------------------------
# Duplicate dedup with real Postgres
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="function")
async def alert_client_real_db(db_pool, settings, temporal_stub):
    app = create_app(run_lifespan=False)
    app.state.db_pool = db_pool
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_workflow_client] = lambda: temporal_stub
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, temporal_stub
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM ingest_idempotency WHERE source_type = 'alertmanager'")


async def test_duplicate_fingerprint_real_db(alert_client_real_db):
    client, temporal = alert_client_real_db
    alert = {
        "status": "firing",
        "labels": {"alertname": "RealDup", "instance": "node-a"},
        "annotations": {"summary": "real dup test"},
        "fingerprint": "am-real-dup-1",
    }
    payload = json.dumps({"alerts": [alert]}).encode()
    r1 = await client.post("/api/webhooks/alert", content=payload)
    r2 = await client.post("/api/webhooks/alert", content=payload)
    assert r1.json()["started"] == 1
    assert r2.json()["started"] == 0
    assert r2.json()["skipped"] == 1
    assert temporal.start_workflow.await_count == 1


# ---------------------------------------------------------------------------
# Optional X-Alert-Token shared secret (#88)
#
# Alertmanager/Grafana don't sign payloads, so this endpoint has no vendor HMAC.
# Setting AEGIS_ALERT_WEBHOOK_SECRET requires a matching header; unset keeps the
# legacy open behaviour.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="function")
async def token_client(temporal_stub):
    """Client factory parametrised by the configured secret."""

    @asynccontextmanager
    async def _build(secret: str):
        settings = Settings(**{**_TEST_SETTINGS, "alert_webhook_secret": secret})
        pool, _ = _mock_pool(fetchval_return="tok-1")
        app = create_app(run_lifespan=False)
        app.state.db_pool = pool
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_workflow_client] = lambda: temporal_stub
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c

    return _build


_FIRING = json.dumps(
    {
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "TokenTest", "instance": "node-a"},
                "annotations": {"summary": "token test"},
                "fingerprint": "tok-1",
            }
        ]
    }
).encode()


async def test_alert_webhook_rejects_missing_token(token_client, temporal_stub):
    async with token_client("s3cret") as c:
        resp = await c.post("/api/webhooks/alert", content=_FIRING)
    assert resp.status_code == 401
    # Rejected before any flow is spawned — that's the whole point.
    temporal_stub.start_workflow.assert_not_awaited()


async def test_alert_webhook_rejects_wrong_token(token_client, temporal_stub):
    async with token_client("s3cret") as c:
        resp = await c.post(
            "/api/webhooks/alert", content=_FIRING, headers={"X-Alert-Token": "wrong"}
        )
    assert resp.status_code == 401
    temporal_stub.start_workflow.assert_not_awaited()


async def test_alert_webhook_accepts_good_token(token_client, temporal_stub):
    async with token_client("s3cret") as c:
        resp = await c.post(
            "/api/webhooks/alert", content=_FIRING, headers={"X-Alert-Token": "s3cret"}
        )
    assert resp.status_code == 200
    assert resp.json()["started"] == 1
    temporal_stub.start_workflow.assert_awaited()


async def test_alert_webhook_open_when_secret_unset(token_client):
    """Backward compatible: no secret configured = no token required."""
    async with token_client("") as c:
        resp = await c.post("/api/webhooks/alert", content=_FIRING)
    assert resp.status_code == 200
    assert resp.json()["started"] == 1
