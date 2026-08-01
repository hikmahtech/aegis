"""/api/admin/people — admin CRUD over the people registry (migration 016).

httpx ASGITransport + a REAL db_pool (TestClient would drive the app on a
second event loop and blow up with asyncpg "another operation is in
progress" the moment a handler touches the database).
"""

from __future__ import annotations

import base64

import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.db import run_migrations
from httpx import ASGITransport, AsyncClient

PREFIX = "zzroute-"


@pytest_asyncio.fixture(loop_scope="function")
async def app(test_settings, db_pool):
    await run_migrations(db_pool)
    await db_pool.execute("DELETE FROM life.people WHERE name LIKE $1", f"{PREFIX}%")
    application = create_app(run_lifespan=False)
    application.dependency_overrides[get_settings] = lambda: test_settings
    application.state.db_pool = db_pool
    yield application
    await db_pool.execute("DELETE FROM life.people WHERE name LIKE $1", f"{PREFIX}%")


@pytest.fixture
def auth_headers():
    creds = base64.b64encode(b"admin:admin").decode()
    return {"Authorization": f"Basic {creds}"}


@pytest_asyncio.fixture(loop_scope="function")
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_people_routes_require_auth(client):
    for method, path in (
        ("get", "/api/admin/people"),
        ("post", "/api/admin/people"),
    ):
        resp = await getattr(client, method)(path, **({"json": {"name": "x"}} if method == "post" else {}))
        assert resp.status_code == 401, f"{method.upper()} {path} -> {resp.status_code}"
        # Assert the AUTH failure specifically: a 401 that isn't the auth
        # dependency's would mean the route is unprotected in a different way.
        assert resp.json()["detail"] == "Invalid credentials"


async def test_crud_round_trip_through_the_routes(client, auth_headers, db_pool):
    resp = await client.post(
        "/api/admin/people",
        headers=auth_headers,
        json={
            "name": f"{PREFIX}Nadia Rahman",
            "aliases": ["Nads", "Nadia.Rahman@Example.com"],
            "relationship": "colleague",
            "key_dates": {"birthday": "1991-06-05"},
            "notes": "prefers email",
            "last_contact": "2026-07-20T10:00:00Z",
        },
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    person_id = created["id"]
    assert created["name"] == f"{PREFIX}Nadia Rahman"
    assert created["relationship"] == "colleague"
    assert created["key_dates"] == {"birthday": "1991-06-05"}
    # Service-level alias normalization survives the route round-trip.
    assert created["aliases"] == ["nads", "nadia.rahman@example.com"]
    assert created["last_contact"].startswith("2026-07-20T10:00:00")

    # The row really is in the database, not just echoed back by the handler.
    assert (
        await db_pool.fetchval("SELECT name FROM life.people WHERE id = $1::uuid", person_id)
        == f"{PREFIX}Nadia Rahman"
    )

    resp = await client.get(f"/api/admin/people/{person_id}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["id"] == person_id

    resp = await client.get("/api/admin/people", headers=auth_headers)
    assert resp.status_code == 200
    assert person_id in [p["id"] for p in resp.json()]

    resp = await client.put(
        f"/api/admin/people/{person_id}",
        headers=auth_headers,
        json={"relationship": "former colleague", "notes": "moved to Berlin"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["relationship"] == "former colleague"
    assert body["notes"] == "moved to Berlin"
    assert body["key_dates"] == {"birthday": "1991-06-05"}
    assert (
        await db_pool.fetchval(
            "SELECT relationship FROM life.people WHERE id = $1::uuid", person_id
        )
        == "former colleague"
    )

    resp = await client.delete(f"/api/admin/people/{person_id}", headers=auth_headers)
    assert resp.status_code == 204
    assert await db_pool.fetchval("SELECT 1 FROM life.people WHERE id = $1::uuid", person_id) is None


async def test_list_q_filters_by_name_or_alias(client, auth_headers):
    resp = await client.post(
        "/api/admin/people",
        headers=auth_headers,
        json={"name": f"{PREFIX}Search Target", "aliases": ["target@example.com"]},
    )
    assert resp.status_code == 201
    target_id = resp.json()["id"]
    resp = await client.post(
        "/api/admin/people", headers=auth_headers, json={"name": f"{PREFIX}Other Person"}
    )
    assert resp.status_code == 201
    other_id = resp.json()["id"]

    resp = await client.get(
        "/api/admin/people", headers=auth_headers, params={"q": "target@example.com"}
    )
    assert resp.status_code == 200
    assert [p["id"] for p in resp.json()] == [target_id]

    # Without ?q= both rows come back — proves the filter above actually
    # filtered rather than the second row simply not existing.
    resp = await client.get("/api/admin/people", headers=auth_headers)
    ids = [p["id"] for p in resp.json()]
    assert target_id in ids and other_id in ids


async def test_missing_person_404s_with_the_handlers_message(client, auth_headers):
    missing = "00000000-0000-0000-0000-0000000000ff"
    for method in ("get", "delete"):
        resp = await getattr(client, method)(f"/api/admin/people/{missing}", headers=auth_headers)
        assert resp.status_code == 404
        # Assert the HANDLER's detail, not a bare 404 — FastAPI 404s an
        # unknown path too, so a status-only assertion would still pass with
        # the route deleted.
        assert resp.json()["detail"] == "Person not found"

    resp = await client.put(
        f"/api/admin/people/{missing}", headers=auth_headers, json={"notes": "ghost"}
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Person not found"


async def test_create_without_a_name_is_a_validation_error(client, auth_headers):
    # Pydantic rejects a missing name outright...
    resp = await client.post("/api/admin/people", headers=auth_headers, json={"relationship": "x"})
    assert resp.status_code == 422
    # ...and a whitespace-only name is caught by the service, surfaced as 400.
    resp = await client.post("/api/admin/people", headers=auth_headers, json={"name": "   "})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "name is required"


async def test_mutations_are_audit_logged(client, auth_headers, db_pool):
    resp = await client.post(
        "/api/admin/people", headers=auth_headers, json={"name": f"{PREFIX}Audited Person"}
    )
    assert resp.status_code == 201
    person_id = resp.json()["id"]

    row = await db_pool.fetchrow(
        "SELECT actor, action, target_type, details FROM audit_log "
        "WHERE target_id = $1 AND action = 'person_created'",
        person_id,
    )
    assert row is not None, "creating a person wrote no audit_log row"
    assert row["actor"] == "api:people_admin"
    assert row["target_type"] == "person"
    assert row["details"] == {"name": f"{PREFIX}Audited Person"}

    resp = await client.delete(f"/api/admin/people/{person_id}", headers=auth_headers)
    assert resp.status_code == 204
    assert await db_pool.fetchval(
        "SELECT 1 FROM audit_log WHERE target_id = $1 AND action = 'person_deleted'",
        person_id,
    )
