"""Webhook routes start Temporal workflows with idempotency (Phase 3).

Tests that exercise the flow-start path use a mock DB pool to avoid
Postgres availability constraints, except the duplicate-delivery test
which needs real INSERT ... ON CONFLICT semantics.
"""

from __future__ import annotations

import hashlib
import hmac
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
    "github_webhook_secret": "testsecret",
    "sentry_webhook_secret": "testsecret",
}


@pytest.fixture
def settings():
    return Settings(**_TEST_SETTINGS)


@pytest.fixture
def temporal_stub():
    handle = MagicMock()
    handle.id = "wf-123"
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
    # resolve_tag("infra") → pandoras-actor (seed mapping), so the workflow
    # still starts with today's agent id.
    pool.fetch = AsyncMock(return_value=[{"id": "pandoras-actor"}])
    return pool, conn


def _sign_github(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _sign_sentry(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# GitHub webhook — flow start
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="function")
async def github_client(settings, temporal_stub):
    pool, _ = _mock_pool(fetchval_return="del-test-1")  # claim succeeds
    app = create_app(run_lifespan=False)
    app.state.db_pool = pool
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_workflow_client] = lambda: temporal_stub
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, temporal_stub


async def test_github_webhook_starts_flow(github_client):
    client, temporal = github_client
    body = json.dumps(
        {"repository": {"full_name": "x/y"}, "workflow_run": {"conclusion": "failure"}}
    ).encode()
    sig = _sign_github("testsecret", body)
    resp = await client.post(
        "/api/webhooks/github",
        content=body,
        headers={
            "X-Hub-Signature-256": sig,
            "X-GitHub-Event": "workflow_run",
            "X-GitHub-Delivery": "del-test-1",
        },
    )
    assert resp.status_code == 200
    out = resp.json()
    assert out["accepted"] is True
    assert out["workflow_id"] == "wf-123"
    assert out["delivery_id"] == "del-test-1"
    temporal.start_workflow.assert_awaited_once()
    call = temporal.start_workflow.call_args
    assert call.args[0] == "GitHubAlertFlow"
    assert call.kwargs["task_queue"] == "aegis-main"
    assert call.kwargs["id"] == "github-del-test-1"
    payload_arg = call.args[1]
    assert payload_arg["agent_id"] == "pandoras-actor"
    assert payload_arg["event"] == "workflow_run"


async def test_github_webhook_bad_signature_returns_401(github_client):
    client, _ = github_client
    resp = await client.post(
        "/api/webhooks/github",
        content=b"{}",
        headers={"X-Hub-Signature-256": "sha256=bad", "X-GitHub-Event": "push"},
    )
    assert resp.status_code == 401


async def test_github_webhook_missing_secret_returns_503(temporal_stub):
    settings_no_secret = Settings(**{**_TEST_SETTINGS, "github_webhook_secret": ""})
    pool, _ = _mock_pool()
    app = create_app(run_lifespan=False)
    app.state.db_pool = pool
    app.dependency_overrides[get_settings] = lambda: settings_no_secret
    app.dependency_overrides[get_workflow_client] = lambda: temporal_stub
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/webhooks/github", content=b"{}")
    assert resp.status_code == 503


async def test_github_webhook_fallback_delivery_id_when_header_missing(github_client):
    """When X-GitHub-Delivery header is absent, a UUID is generated as delivery_id."""
    client, temporal = github_client
    body = b"{}"
    sig = _sign_github("testsecret", body)
    resp = await client.post(
        "/api/webhooks/github",
        content=body,
        headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "push"},
    )
    assert resp.status_code == 200
    out = resp.json()
    assert out["accepted"] is True
    assert out["workflow_id"] == "wf-123"
    # delivery_id present in response since we always return it
    assert "delivery_id" in out


# ---------------------------------------------------------------------------
# GitHub webhook — duplicate delivery (needs real Postgres)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="function")
async def github_client_real_db(db_pool, settings, temporal_stub):
    # Ensure some active agent holds the `infra` tag so resolve_tag("infra")
    # succeeds and the (non-duplicate) first delivery starts its workflow —
    # independent of whatever the shared dev DB's seed agents carry.
    await db_pool.execute(
        """
        INSERT INTO agents (id, name, role, system_prompt_path, capabilities,
                            model_tier, metadata, active)
        VALUES ('tagtest-webhook-infra', 'tagtest-webhook-infra', 'test', '',
                '["infra"]'::jsonb, 'balanced', '{}'::jsonb, TRUE)
        ON CONFLICT (id) DO NOTHING
        """
    )
    app = create_app(run_lifespan=False)
    app.state.db_pool = db_pool
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_workflow_client] = lambda: temporal_stub
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, temporal_stub
    # Cleanup
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM ingest_idempotency WHERE source_type IN ('github', 'sentry')"
        )
        await conn.execute("DELETE FROM agents WHERE id = 'tagtest-webhook-infra'")


async def test_github_webhook_duplicate_delivery_returns_200_noop(github_client_real_db):
    client, temporal = github_client_real_db
    body = b'{"repository": {"full_name": "x/y"}}'
    sig = _sign_github("testsecret", body)
    headers = {
        "X-Hub-Signature-256": sig,
        "X-GitHub-Event": "push",
        "X-GitHub-Delivery": "del-dup-phase3",
    }
    r1 = await client.post("/api/webhooks/github", content=body, headers=headers)
    r2 = await client.post("/api/webhooks/github", content=body, headers=headers)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.json().get("duplicate") is True
    # Only ONE flow started for two identical deliveries
    assert temporal.start_workflow.await_count == 1


# ---------------------------------------------------------------------------
# Sentry webhook — flow start
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="function")
async def sentry_client(settings, temporal_stub):
    pool, _ = _mock_pool(fetchval_return="sentry:issue-42")  # claim succeeds
    app = create_app(run_lifespan=False)
    app.state.db_pool = pool
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_workflow_client] = lambda: temporal_stub
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, temporal_stub


async def test_sentry_webhook_starts_flow(sentry_client):
    client, temporal = sentry_client
    body = json.dumps({"data": {"issue": {"id": "issue-42", "title": "Crash"}}}).encode()
    sig = _sign_sentry("testsecret", body)
    resp = await client.post(
        "/api/webhooks/sentry",
        content=body,
        headers={"Sentry-Hook-Signature": sig},
    )
    assert resp.status_code == 200
    out = resp.json()
    assert out["accepted"] is True
    assert out["workflow_id"] == "wf-123"
    assert out["issue_id"] == "issue-42"
    call = temporal.start_workflow.call_args
    assert call.args[0] == "SentryPollFlow"
    assert call.kwargs["id"] == "sentry-alert-issue-42"
    assert call.kwargs["task_queue"] == "aegis-main"
    payload_arg = call.args[1]
    assert payload_arg["agent_id"] == "pandoras-actor"
    assert payload_arg["mode"] == "webhook"


async def test_sentry_webhook_bad_signature_returns_401(sentry_client):
    client, _ = sentry_client
    body = b'{"data": {"issue": {"id": "x"}}}'
    resp = await client.post(
        "/api/webhooks/sentry",
        content=body,
        headers={"Sentry-Hook-Signature": "badsig"},
    )
    assert resp.status_code == 401


async def test_sentry_webhook_missing_secret_returns_503(temporal_stub):
    settings_no_secret = Settings(**{**_TEST_SETTINGS, "sentry_webhook_secret": ""})
    pool, _ = _mock_pool()
    app = create_app(run_lifespan=False)
    app.state.db_pool = pool
    app.dependency_overrides[get_settings] = lambda: settings_no_secret
    app.dependency_overrides[get_workflow_client] = lambda: temporal_stub
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/webhooks/sentry", content=b"{}")
    assert resp.status_code == 503


async def test_sentry_webhook_no_issue_id_returns_skipped(sentry_client):
    client, temporal = sentry_client
    body = json.dumps({"actor": {"type": "user"}}).encode()
    sig = _sign_sentry("testsecret", body)
    resp = await client.post(
        "/api/webhooks/sentry",
        content=body,
        headers={"Sentry-Hook-Signature": sig},
    )
    assert resp.status_code == 200
    assert resp.json()["skipped"] == "no_issue_id"
    temporal.start_workflow.assert_not_called()


async def test_sentry_webhook_duplicate_delivery_returns_200_noop(github_client_real_db):
    """Re-uses the real db fixture since it cleans up both source_types."""
    client, temporal = github_client_real_db
    body = json.dumps({"data": {"issue": {"id": "issue-sentry-dup", "title": "Oops"}}}).encode()
    sig = _sign_sentry("testsecret", body)
    headers = {"Sentry-Hook-Signature": sig}
    r1 = await client.post("/api/webhooks/sentry", content=body, headers=headers)
    r2 = await client.post("/api/webhooks/sentry", content=body, headers=headers)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.json().get("duplicate") is True
    # Start count: 1 from github dup test (if run together) + 1 from sentry
    # Just assert total <= 2 and second sentry call didn't add another
    sentry_calls = [
        c for c in temporal.start_workflow.call_args_list if c.args[0] == "SentryPollFlow"
    ]
    assert len(sentry_calls) == 1
