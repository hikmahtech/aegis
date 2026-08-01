"""GET /api/admin/agents/{id}/personality/revisions — ops-only revision log."""

from __future__ import annotations

import base64

import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.db import run_migrations
from aegis.services import personalities as p
from httpx import ASGITransport, AsyncClient

AGENT = "zzrev-route"


@pytest_asyncio.fixture(loop_scope="function")
async def app(test_settings, db_pool):
    await run_migrations(db_pool)
    await db_pool.execute("DELETE FROM agents WHERE id = $1", AGENT)
    await db_pool.execute(
        "INSERT INTO agents (id, name, role, system_prompt_path, active) "
        "VALUES ($1, 'Z', 'r', '', true)",
        AGENT,
    )
    p.invalidate()
    application = create_app(run_lifespan=False)
    application.dependency_overrides[get_settings] = lambda: test_settings
    application.state.db_pool = db_pool
    yield application
    await db_pool.execute("DELETE FROM agents WHERE id = $1", AGENT)
    p.invalidate()


@pytest.fixture
def auth_headers():
    creds = base64.b64encode(b"admin:admin").decode()
    return {"Authorization": f"Basic {creds}"}


async def test_revisions_require_auth(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/api/admin/agents/{AGENT}/personality/revisions")
        assert resp.status_code == 401


async def test_revisions_returns_log_newest_first(app, auth_headers, db_pool):
    await p.apply_profile_patch(db_pool, AGENT, "user", "first", source="s1")
    await p.apply_profile_patch(db_pool, AGENT, "user", "first and second", source="s2")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/api/admin/agents/{AGENT}/personality/revisions", headers=auth_headers
        )
        assert resp.status_code == 200
        body = resp.json()
        assert [r["source"] for r in body] == ["s2", "s1"]
        assert body[0]["before_content"] == "first"
        assert body[0]["after_content"] == "first and second"
        assert body[0]["kind"] == "user"
        assert body[0]["agent_id"] == AGENT
        assert body[0]["interaction_id"] is None
        assert body[0]["created_at"]

        # kind filter + limit are honoured.
        resp = await client.get(
            f"/api/admin/agents/{AGENT}/personality/revisions",
            headers=auth_headers,
            params={"kind": "user", "limit": 1},
        )
        assert resp.status_code == 200 and len(resp.json()) == 1

        resp = await client.get(
            f"/api/admin/agents/{AGENT}/personality/revisions",
            headers=auth_headers,
            params={"kind": "memory"},
        )
        assert resp.status_code == 200 and resp.json() == []


async def test_revisions_bad_kind_400(app, auth_headers):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/api/admin/agents/{AGENT}/personality/revisions",
            headers=auth_headers,
            params={"kind": "vibes"},
        )
        assert resp.status_code == 400
        assert "unknown personality kind" in resp.json()["detail"]


async def test_revisions_unknown_agent_404(app, auth_headers):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/admin/agents/zz-does-not-exist/personality/revisions", headers=auth_headers
        )
        assert resp.status_code == 404
        # Assert the HANDLER's 404, not FastAPI's no-such-route 404 ("Not
        # Found") — a status-code-only assertion passes even with the route
        # deleted, which is the unfalsifiable-test trap.
        assert resp.json()["detail"] == "Agent 'zz-does-not-exist' not found"


def test_route_docstring_marks_it_ops_only():
    """Issue #101 convention: endpoints deliberately not wired to the admin
    panel say so, so a later reader doesn't 'fix' the missing UI."""
    from aegis.api.routes.agents import get_agent_personality_revisions

    assert "intentionally curl/ops-only, no UI consumer" in (
        get_agent_personality_revisions.__doc__ or ""
    )
