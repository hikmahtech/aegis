"""/api/admin/expiring-items — admin CRUD over the expiry registry (migration 018).

httpx ASGITransport + a REAL db_pool (TestClient would drive the app on a
second event loop and blow up with asyncpg "another operation is in
progress" the moment a handler touches the database).
"""

from __future__ import annotations

import base64
from datetime import timedelta

import asyncpg
import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.db import run_migrations
from httpx import ASGITransport, AsyncClient

PREFIX = "zzexproute-"


async def _wipe(pool: asyncpg.Pool) -> None:
    # Children before parents: alerts -> items -> people.
    await pool.execute(
        "DELETE FROM life.expiring_item_alerts WHERE item_id IN "
        "(SELECT id FROM life.expiring_items WHERE title LIKE $1)",
        f"{PREFIX}%",
    )
    await pool.execute("DELETE FROM life.expiring_items WHERE title LIKE $1", f"{PREFIX}%")
    await pool.execute("DELETE FROM life.people WHERE name LIKE $1", f"{PREFIX}%")


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


@pytest_asyncio.fixture(loop_scope="function")
async def today(db_pool):
    """The database's CURRENT_DATE — the clock `due_within` compares against."""
    return await db_pool.fetchval("SELECT CURRENT_DATE")


async def test_expiring_item_routes_require_auth(client):
    for method, path in (
        ("get", "/api/admin/expiring-items"),
        ("post", "/api/admin/expiring-items"),
    ):
        payload = {"kind": "visa", "title": "x", "expires_on": "2030-01-01"}
        resp = await getattr(client, method)(
            path, **({"json": payload} if method == "post" else {})
        )
        assert resp.status_code == 401, f"{method.upper()} {path} -> {resp.status_code}"
        # Assert the AUTH failure specifically: a 401 that isn't the auth
        # dependency's would mean the route is unprotected in a different way.
        assert resp.json()["detail"] == "Invalid credentials"


async def test_crud_round_trip_through_the_routes(client, auth_headers, db_pool, today):
    person_id = await db_pool.fetchval(
        "INSERT INTO life.people (name) VALUES ($1) RETURNING id", f"{PREFIX}Owner"
    )
    expiry = (today + timedelta(days=120)).isoformat()
    resp = await client.post(
        "/api/admin/expiring-items",
        headers=auth_headers,
        json={
            "kind": "Passport",
            "title": f"{PREFIX}Indian passport",
            "expires_on": expiry,
            "lead_days": [7, 90, 7],
            "person_id": str(person_id),
            "notes": "renew at the PSK",
        },
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    item_id = created["id"]
    assert created["title"] == f"{PREFIX}Indian passport"
    assert created["expires_on"] == expiry
    # Service-level normalization survives the route round-trip.
    assert created["kind"] == "passport"
    assert created["lead_days"] == [90, 7]
    assert created["person_id"] == str(person_id)

    # The row really is in the database, not just echoed back by the handler.
    assert (
        await db_pool.fetchval(
            "SELECT title FROM life.expiring_items WHERE id = $1::uuid", item_id
        )
        == f"{PREFIX}Indian passport"
    )

    resp = await client.get(f"/api/admin/expiring-items/{item_id}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["id"] == item_id

    resp = await client.get("/api/admin/expiring-items", headers=auth_headers)
    assert resp.status_code == 200
    assert item_id in [i["id"] for i in resp.json()]

    renewed = (today + timedelta(days=3650)).isoformat()
    resp = await client.put(
        f"/api/admin/expiring-items/{item_id}",
        headers=auth_headers,
        json={"expires_on": renewed, "notes": "renewed"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["expires_on"] == renewed
    assert body["notes"] == "renewed"
    assert body["lead_days"] == [90, 7]
    assert (
        await db_pool.fetchval(
            "SELECT expires_on::text FROM life.expiring_items WHERE id = $1::uuid", item_id
        )
        == renewed
    )

    resp = await client.delete(f"/api/admin/expiring-items/{item_id}", headers=auth_headers)
    assert resp.status_code == 204
    assert (
        await db_pool.fetchval("SELECT 1 FROM life.expiring_items WHERE id = $1::uuid", item_id)
        is None
    )


async def test_list_filters_by_kind_and_by_due_window(client, auth_headers, today):
    async def make(kind: str, title: str, days: int) -> str:
        resp = await client.post(
            "/api/admin/expiring-items",
            headers=auth_headers,
            json={
                "kind": kind,
                "title": f"{PREFIX}{title}",
                "expires_on": (today + timedelta(days=days)).isoformat(),
            },
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    soon = await make("visa", "soon", 10)
    far = await make("visa", "far", 400)
    other = await make("domain", "other", 10)

    resp = await client.get(
        "/api/admin/expiring-items", headers=auth_headers, params={"kind": "visa"}
    )
    assert resp.status_code == 200
    by_kind = [i["id"] for i in resp.json()]
    assert soon in by_kind and far in by_kind
    assert other not in by_kind

    resp = await client.get(
        "/api/admin/expiring-items", headers=auth_headers, params={"due_within": 30}
    )
    assert resp.status_code == 200
    windowed = {i["id"]: i for i in resp.json()}
    assert soon in windowed and other in windowed
    assert far not in windowed
    # due_within carries the computed days_left the plain list does not.
    assert windowed[soon]["days_left"] == 10

    # Unfiltered returns all three — proves both filters actually filtered
    # rather than the excluded rows simply not existing.
    resp = await client.get("/api/admin/expiring-items", headers=auth_headers)
    all_ids = [i["id"] for i in resp.json()]
    for item_id in (soon, far, other):
        assert item_id in all_ids
    assert "days_left" not in resp.json()[0]


async def test_missing_item_404s_with_the_handlers_message(client, auth_headers):
    missing = "00000000-0000-0000-0000-0000000000fe"
    for method in ("get", "delete"):
        resp = await getattr(client, method)(
            f"/api/admin/expiring-items/{missing}", headers=auth_headers
        )
        assert resp.status_code == 404
        # Assert the HANDLER's detail, not a bare 404 — FastAPI 404s an
        # unknown path too, so a status-only assertion would still pass with
        # the route deleted.
        assert resp.json()["detail"] == "Expiring item not found"

    resp = await client.put(
        f"/api/admin/expiring-items/{missing}", headers=auth_headers, json={"notes": "ghost"}
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Expiring item not found"


async def test_create_validation_errors(client, auth_headers):
    # Pydantic rejects a missing required field outright...
    resp = await client.post(
        "/api/admin/expiring-items", headers=auth_headers, json={"kind": "visa"}
    )
    assert resp.status_code == 422
    # ...and a whitespace-only kind is caught by the service, surfaced as 400.
    resp = await client.post(
        "/api/admin/expiring-items",
        headers=auth_headers,
        json={"kind": "   ", "title": f"{PREFIX}x", "expires_on": "2030-01-01"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "kind is required"


async def test_mutations_are_audit_logged(client, auth_headers, db_pool):
    resp = await client.post(
        "/api/admin/expiring-items",
        headers=auth_headers,
        json={
            "kind": "warranty",
            "title": f"{PREFIX}Audited item",
            "expires_on": "2030-05-05",
        },
    )
    assert resp.status_code == 201
    item_id = resp.json()["id"]

    row = await db_pool.fetchrow(
        "SELECT actor, action, target_type, details FROM audit_log "
        "WHERE target_id = $1 AND action = 'expiring_item_created'",
        item_id,
    )
    assert row is not None, "creating an expiring item wrote no audit_log row"
    assert row["actor"] == "api:expiring_items_admin"
    assert row["target_type"] == "expiring_item"
    assert row["details"] == {"kind": "warranty", "title": f"{PREFIX}Audited item"}

    resp = await client.delete(f"/api/admin/expiring-items/{item_id}", headers=auth_headers)
    assert resp.status_code == 204
    assert await db_pool.fetchval(
        "SELECT 1 FROM audit_log WHERE target_id = $1 AND action = 'expiring_item_deleted'",
        item_id,
    )
