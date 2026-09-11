"""/api/admin/runbooks — admin CRUD over the runbooks table (#499).

httpx ASGITransport + a REAL db_pool (TestClient would drive the app on a
second event loop and blow up with asyncpg "another operation is in progress"
the moment a handler touches the database).
"""

from __future__ import annotations

import base64
from urllib.parse import quote

import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.db import run_migrations
from aegis.services import runbooks as rb
from httpx import ASGITransport, AsyncClient

PREFIX = "zzrbroute"
BASE = "/api/admin/runbooks"


async def _wipe(pool) -> None:
    await pool.execute("DELETE FROM runbooks WHERE name_key LIKE $1", f"{PREFIX}%")
    await pool.execute(
        "DELETE FROM audit_log WHERE target_type = 'runbook' AND target_id LIKE $1", f"{PREFIX}%"
    )


@pytest_asyncio.fixture(loop_scope="function")
async def app(test_settings, db_pool):
    await run_migrations(db_pool)
    await _wipe(db_pool)
    application = create_app(run_lifespan=False)
    application.dependency_overrides[get_settings] = lambda: test_settings
    application.state.db_pool = db_pool
    yield application
    await _wipe(db_pool)


@pytest.fixture
def auth_headers():
    creds = base64.b64encode(b"admin:admin").decode()
    return {"Authorization": f"Basic {creds}"}


@pytest_asyncio.fixture(loop_scope="function")
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _url(name: str) -> str:
    return f"{BASE}/{quote(name, safe='')}"


async def test_runbook_routes_require_auth(client):
    for method, path, kwargs in (
        ("get", BASE, {}),
        ("get", _url(f"{PREFIX}NodeDown"), {}),
        ("put", _url(f"{PREFIX}NodeDown"), {"json": {"body": "x"}}),
        ("delete", _url(f"{PREFIX}NodeDown"), {}),
    ):
        resp = await getattr(client, method)(path, **kwargs)
        assert resp.status_code == 401, f"{method.upper()} {path} -> {resp.status_code}"
        assert resp.json()["detail"] == "Invalid credentials"


async def test_put_get_list_delete_round_trip(client, auth_headers, db_pool):
    name = f"{PREFIX} Pipeline Failure"  # a Grafana-style title, spaces and all
    resp = await client.put(
        _url(name),
        json={"body": "# Runbook\n\nCheck the run first.\n", "updated_by": "loader"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    saved = resp.json()
    assert saved["name"] == name
    assert saved["name_key"] == f"{PREFIX}pipelinefailure"
    assert saved["body"] == "# Runbook\n\nCheck the run first."
    assert saved["updated_by"] == "loader"
    assert saved["created"] is True

    # The hub's slug of the same alert name reads the same row.
    resp = await client.get(_url(f"{PREFIX}-pipeline-failure"), headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["body"] == "# Runbook\n\nCheck the run first."

    resp = await client.get(BASE, headers=auth_headers)
    assert resp.status_code == 200
    mine = [r for r in resp.json() if r["name_key"].startswith(PREFIX)]
    assert [(r["name"], r["chars"]) for r in mine] == [(name, len(saved["body"]))]

    resp = await client.delete(_url(f"{PREFIX}PipelineFailure"), headers=auth_headers)
    assert resp.status_code == 204
    resp = await client.get(_url(name), headers=auth_headers)
    assert resp.status_code == 404

    actions = [
        r["action"]
        for r in await db_pool.fetch(
            "SELECT action FROM audit_log WHERE target_type = 'runbook' AND target_id = $1 "
            "ORDER BY created_at",
            f"{PREFIX}pipelinefailure",
        )
    ]
    assert actions == ["runbook_saved", "runbook_deleted"]


async def test_put_without_updated_by_records_the_admin_surface(client, auth_headers):
    resp = await client.put(_url(f"{PREFIX}Quiet"), json={"body": "b"}, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["updated_by"] == "admin"


@pytest.mark.parametrize(
    ("name", "body"),
    [
        (f"{PREFIX}Blank", "   "),
        (f"{PREFIX}Stub", "# Stub\n\nTODO: fill in"),
        (f"{PREFIX}Huge", "x" * (rb.MAX_BODY_CHARS + 1)),
        ("---", "a body with no name to hang it on"),
    ],
)
async def test_put_answers_400_instead_of_saving_a_runbook_that_does_nothing(
    client, auth_headers, db_pool, name, body
):
    resp = await client.put(_url(name), json={"body": body}, headers=auth_headers)
    assert resp.status_code == 400, resp.text
    assert await rb.get_runbook(db_pool, name) is None


async def test_get_and_delete_of_a_missing_runbook_are_404(client, auth_headers):
    assert (await client.get(_url(f"{PREFIX}Nope"), headers=auth_headers)).status_code == 404
    assert (await client.delete(_url(f"{PREFIX}Nope"), headers=auth_headers)).status_code == 404
