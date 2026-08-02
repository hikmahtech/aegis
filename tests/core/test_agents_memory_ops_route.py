"""GET /api/admin/agents/{id}/memory/ops — the A4 consolidation ledger.

This is the endpoint an operator reads for a week of dry-run output before
deciding whether to open the apply gate, so its contract matters: it must
show refused ops and their reasons, not just the ones that went through.
"""

from __future__ import annotations

import base64

import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.db import run_migrations
from httpx import ASGITransport, AsyncClient

AGENT = "zza4-ops-route"
OTHER = "zza4-ops-other"


@pytest_asyncio.fixture(loop_scope="function")
async def app(test_settings, db_pool):
    await run_migrations(db_pool)
    for aid in (AGENT, OTHER):
        await db_pool.execute("DELETE FROM agent_memory_ops_log WHERE agent_id = $1", aid)
        await db_pool.execute("DELETE FROM agents WHERE id = $1", aid)
        await db_pool.execute(
            "INSERT INTO agents (id, name, role, system_prompt_path, active) "
            "VALUES ($1, 'Z', 'r', '', true)",
            aid,
        )
    application = create_app(run_lifespan=False)
    application.dependency_overrides[get_settings] = lambda: test_settings
    application.state.db_pool = db_pool
    yield application
    for aid in (AGENT, OTHER):
        await db_pool.execute("DELETE FROM agent_memory_ops_log WHERE agent_id = $1", aid)
        await db_pool.execute("DELETE FROM agents WHERE id = $1", aid)


@pytest.fixture
def auth_headers():
    creds = base64.b64encode(b"admin:admin").decode()
    return {"Authorization": f"Basic {creds}"}


async def _log(pool, agent_id, op, *, applied, dry_run, skip_reason=None, before="old"):
    await pool.execute(
        "INSERT INTO agent_memory_ops_log (agent_id, run_id, op, memory_id, before_content, "
        "after_content, dry_run, applied, skip_reason) VALUES ($1,'run-1',$2,42,$3,'new',$4,$5,$6)",
        agent_id,
        op,
        before,
        dry_run,
        applied,
        skip_reason,
    )


async def test_memory_ops_require_auth(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get(f"/api/admin/agents/{AGENT}/memory/ops")).status_code == 401


async def test_memory_ops_returns_refused_ops_with_reasons(app, auth_headers, db_pool):
    await _log(db_pool, AGENT, "DELETE", applied=False, dry_run=True, skip_reason="quota_exceeded_pct")
    await _log(db_pool, AGENT, "UPDATE", applied=True, dry_run=False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/api/admin/agents/{AGENT}/memory/ops", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert [r["op"] for r in body] == ["UPDATE", "DELETE"], "newest first"
        refused = body[1]
        assert refused["applied"] is False
        assert refused["dry_run"] is True
        assert refused["skip_reason"] == "quota_exceeded_pct"
        assert refused["before_content"] == "old"
        assert refused["after_content"] == "new"
        assert refused["run_id"] == "run-1"
        assert refused["memory_id"] == 42

        # applied_only is the "what actually changed" view.
        resp = await client.get(
            f"/api/admin/agents/{AGENT}/memory/ops",
            headers=auth_headers,
            params={"applied_only": True},
        )
        assert [r["op"] for r in resp.json()] == ["UPDATE"]


async def test_memory_ops_are_scoped_to_the_agent(app, auth_headers, db_pool):
    await _log(db_pool, OTHER, "DELETE", applied=True, dry_run=False, before="someone else's")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/api/admin/agents/{AGENT}/memory/ops", headers=auth_headers)
        assert resp.json() == []
        resp = await client.get(f"/api/admin/agents/{OTHER}/memory/ops", headers=auth_headers)
        assert len(resp.json()) == 1


async def test_memory_ops_limit_is_clamped_into_range(app, auth_headers, db_pool):
    for _ in range(4):
        await _log(db_pool, AGENT, "NOOP", applied=False, dry_run=True)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            f"/api/admin/agents/{AGENT}/memory/ops", headers=auth_headers, params={"limit": 2}
        )
        assert len(resp.json()) == 2
        # Clamped up from 0 — an unclamped `LIMIT 0` would return nothing and a
        # negative one would be a Postgres error, so this is the observable
        # half of the clamp. (The 500 ceiling needs 500 rows to see and is not
        # worth the fixture cost.)
        resp = await client.get(
            f"/api/admin/agents/{AGENT}/memory/ops", headers=auth_headers, params={"limit": 0}
        )
        assert resp.status_code == 200 and len(resp.json()) == 1


async def test_memory_ops_unknown_agent_404(app, auth_headers):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            "/api/admin/agents/zz-does-not-exist/memory/ops", headers=auth_headers
        )
        assert resp.status_code == 404
        # The HANDLER's 404, not FastAPI's no-such-route 404 — a status-only
        # assertion passes even with the route deleted.
        assert resp.json()["detail"] == "Agent 'zz-does-not-exist' not found"


def test_route_docstring_marks_it_ops_only():
    """Issue #101 convention."""
    from aegis.api.routes.agents import get_agent_memory_ops

    assert "intentionally curl/ops-only, no UI consumer" in (get_agent_memory_ops.__doc__ or "")
