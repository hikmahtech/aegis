"""Interactions API route tests.

Mocks the Temporal client's signal path using the get_workflow_client
dependency override.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.api.routes.interactions import get_workflow_client
from aegis.config import Settings
from httpx import ASGITransport, AsyncClient

_TEST_REQUIRED_SETTINGS = {
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
    return Settings(**_TEST_REQUIRED_SETTINGS)


@pytest.fixture
def auth_headers():
    creds = base64.b64encode(b"admin:admin").decode()
    return {"Authorization": f"Basic {creds}"}


@pytest_asyncio.fixture(loop_scope="function")
async def app_with_fake_temporal(db_pool, settings):
    app = create_app(run_lifespan=False)
    app.state.db_pool = db_pool

    fake_client = AsyncMock()
    fake_handle = AsyncMock()
    fake_client.get_workflow_handle = lambda wid: fake_handle

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_workflow_client] = lambda: fake_client

    yield app, fake_client, fake_handle

    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM interactions")


@pytest_asyncio.fixture(loop_scope="function")
async def seeded_agent(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agents (id, name, role, system_prompt_path, active) "
            "VALUES ('sebas', 'Sebas', 'assistant', 'personalities/sebas', TRUE) "
            "ON CONFLICT (id) DO NOTHING"
        )
    yield


async def test_resolve_interaction_signals_workflow(
    app_with_fake_temporal, db_pool, seeded_agent, auth_headers
):
    app, fake_client, fake_handle = app_with_fake_temporal

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO interactions "
            "(flow_run_id, agent_id, kind, origin, prompt, status, timeout_policy) "
            "VALUES ('flow-run-123', 'sebas', 'approval', 'test', 'x', 'pending', 'archive') "
            "RETURNING id"
        )
        interaction_id = str(row["id"])

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/interactions/{interaction_id}/resolve",
            json={"response": {"value": "approved"}},
            headers=auth_headers,
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "resolved"
    assert body["already_resolved"] is False

    async with db_pool.acquire() as conn:
        db_row = await conn.fetchrow(
            "SELECT status, response FROM interactions WHERE id = $1",
            UUID(interaction_id),
        )
    assert db_row["status"] == "resolved"
    resp_col = db_row["response"]
    if isinstance(resp_col, str):
        resp_col = json.loads(resp_col)
    assert resp_col == {"value": "approved"}

    fake_handle.signal.assert_awaited_once()
    call = fake_handle.signal.await_args
    # First positional arg is the signal name (str); second is the dict payload
    assert call.args[0] == "submit_response"
    assert call.args[1] == {"value": "approved"}


async def test_resolve_returns_404_for_unknown_id(app_with_fake_temporal, auth_headers):
    app, _, _ = app_with_fake_temporal
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/interactions/{uuid4()}/resolve",
            json={"response": {}},
            headers=auth_headers,
        )
    assert resp.status_code == 404


async def test_resolve_returns_422_for_malformed_id(app_with_fake_temporal, auth_headers):
    app, _, _ = app_with_fake_temporal
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/interactions/not-a-uuid/resolve",
            json={"response": {}},
            headers=auth_headers,
        )
    assert resp.status_code == 422


async def test_list_rejects_unknown_status(app_with_fake_temporal, auth_headers):
    app, _, _ = app_with_fake_temporal
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/interactions?status=bogus",
            headers=auth_headers,
        )
    assert resp.status_code == 422


async def test_resolve_is_idempotent_on_already_resolved(
    app_with_fake_temporal, db_pool, seeded_agent, auth_headers
):
    app, _, fake_handle = app_with_fake_temporal
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO interactions "
            "(flow_run_id, agent_id, kind, origin, prompt, status, response, "
            " resolved_at, timeout_policy) "
            "VALUES ('r', 'sebas', 'approval', 'o', 'p', 'resolved', "
            " '{\"v\": 1}'::jsonb, now(), 'archive') RETURNING id"
        )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/interactions/{row['id']}/resolve",
            json={"response": {"v": 2}},
            headers=auth_headers,
        )
    assert resp.status_code == 200
    assert resp.json()["already_resolved"] is True
    fake_handle.signal.assert_not_called()


async def test_resolve_update_does_not_clobber_archived_row(
    app_with_fake_temporal, db_pool, seeded_agent, auth_headers
):
    """End-to-end double-resolve protection: archived row stays archived
    even if /resolve is called against it. The SELECT-then-early-return
    handles the common case; the `AND status='pending'` guard on the
    UPDATE protects the residual race between SELECT and UPDATE.

    We can't easily wedge a concurrent flip between SELECT and UPDATE in
    a unit test, but this end-to-end shape — call /resolve on a non-pending
    row, see `already_resolved=True`, no signal, no row mutation —
    captures the user-visible invariant the UPDATE guard underwrites.
    """
    app, _, fake_handle = app_with_fake_temporal
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO interactions "
            "(flow_run_id, agent_id, kind, origin, prompt, status, timeout_policy) "
            "VALUES ('r2', 'sebas', 'approval', 'o', 'p', 'archived', 'archive') "
            "RETURNING id"
        )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/interactions/{row['id']}/resolve",
            json={"response": {"v": 99}},
            headers=auth_headers,
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["already_resolved"] is True
    fake_handle.signal.assert_not_called()

    # Row is still archived; response column was never overwritten.
    async with db_pool.acquire() as conn:
        db_row = await conn.fetchrow(
            "SELECT status, response FROM interactions WHERE id = $1", row["id"]
        )
    assert db_row["status"] == "archived"
    assert db_row["response"] is None


def test_resolve_update_sql_has_status_guard():
    """Pin test: the UPDATE in resolve_interaction must include
    `AND status = 'pending'`. Protects against a future refactor reverting
    the guard, which would re-open the double-resolve clobber race."""
    import inspect

    from aegis.api.routes import interactions as mod

    src = inspect.getsource(mod.resolve_interaction)
    # Normalise whitespace for a robust substring check
    flat = " ".join(src.split())
    assert "AND status = 'pending'" in flat
    assert "RETURNING id" in flat
