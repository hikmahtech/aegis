"""/api/admin/assets — admin CRUD over the household/asset registry (019).

httpx ASGITransport + a REAL db_pool (TestClient would drive the app on a
second event loop and blow up with asyncpg "another operation is in
progress" the moment a handler touches the database).
"""

from __future__ import annotations

import base64

import asyncpg
import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.db import run_migrations
from httpx import ASGITransport, AsyncClient

PREFIX = "zzc7route-"


async def _wipe(pool: asyncpg.Pool) -> None:
    # Children before parents: alerts -> expiring_items -> assets.
    await pool.execute(
        "DELETE FROM life.expiring_item_alerts WHERE item_id IN "
        "(SELECT id FROM life.expiring_items WHERE title LIKE $1 "
        " OR asset_id IN (SELECT id FROM life.assets WHERE slug LIKE $2))",
        f"%{PREFIX}%",
        f"{PREFIX}%",
    )
    await pool.execute(
        "DELETE FROM life.expiring_items WHERE title LIKE $1 "
        "OR asset_id IN (SELECT id FROM life.assets WHERE slug LIKE $2)",
        f"%{PREFIX}%",
        f"{PREFIX}%",
    )
    await pool.execute("DELETE FROM life.assets WHERE slug LIKE $1", f"{PREFIX}%")


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


async def test_asset_routes_require_auth(client):
    for method, path in (("get", "/api/admin/assets"), ("post", "/api/admin/assets")):
        payload = {"name": "x", "kind": "car"}
        resp = await getattr(client, method)(
            path, **({"json": payload} if method == "post" else {})
        )
        assert resp.status_code == 401, f"{method.upper()} {path} -> {resp.status_code}"
        # Assert the AUTH failure specifically: a 401 that isn't the auth
        # dependency's would mean the route is unprotected in a different way.
        assert resp.json()["detail"] == "Invalid credentials"


async def test_crud_round_trip_through_the_routes(client, auth_headers, db_pool):
    resp = await client.post(
        "/api/admin/assets",
        headers=auth_headers,
        json={
            "name": f"{PREFIX}Bosch Washer",
            "kind": "Appliance",
            "purchase_date": "2022-03-01",
            "warranty_until": "2027-03-01",
            "location": "utility room",
            "notes": "model WAT24",
        },
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    asset_id = created["id"]
    # Service-level normalization survives the route round-trip.
    assert created["kind"] == "appliance"
    assert created["slug"] == f"{PREFIX}bosch-washer"
    assert created["purchase_date"] == "2022-03-01"

    # The row really is in the database, not just echoed back by the handler.
    assert (
        await db_pool.fetchval("SELECT name FROM life.assets WHERE id = $1::uuid", asset_id)
        == f"{PREFIX}Bosch Washer"
    )

    resp = await client.get(f"/api/admin/assets/{asset_id}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["id"] == asset_id

    resp = await client.get("/api/admin/assets", headers=auth_headers)
    assert resp.status_code == 200
    assert asset_id in [a["id"] for a in resp.json()]

    resp = await client.put(
        f"/api/admin/assets/{asset_id}",
        headers=auth_headers,
        json={"location": "garage", "notes": "moved"},
    )
    assert resp.status_code == 200
    assert resp.json()["location"] == "garage"
    assert (
        await db_pool.fetchval("SELECT location FROM life.assets WHERE id = $1::uuid", asset_id)
        == "garage"
    )

    resp = await client.delete(f"/api/admin/assets/{asset_id}", headers=auth_headers)
    assert resp.status_code == 204
    assert (
        await db_pool.fetchval("SELECT 1 FROM life.assets WHERE id = $1::uuid", asset_id) is None
    )


async def test_list_filters_by_kind(client, auth_headers):
    async def make(name: str, kind: str) -> str:
        resp = await client.post(
            "/api/admin/assets", headers=auth_headers, json={"name": f"{PREFIX}{name}", "kind": kind}
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    car = await make("Volvo", "car")
    fridge = await make("Fridge", "appliance")

    resp = await client.get("/api/admin/assets", headers=auth_headers, params={"kind": "car"})
    assert resp.status_code == 200
    ids = [a["id"] for a in resp.json()]
    assert car in ids
    assert fridge not in ids

    # Unfiltered returns both — proves the filter actually filtered rather
    # than the excluded row simply not existing.
    resp = await client.get("/api/admin/assets", headers=auth_headers)
    all_ids = [a["id"] for a in resp.json()]
    assert car in all_ids and fridge in all_ids


async def test_service_schedule_round_trips_into_the_expiry_radar(client, auth_headers, db_pool):
    """The whole point of the feature, driven end to end through the routes:
    an asset with a service schedule shows up on the Expiry Radar's own API."""
    resp = await client.post(
        "/api/admin/assets",
        headers=auth_headers,
        json={
            "name": f"{PREFIX}Boiler",
            "kind": "hvac",
            "service_interval_days": 365,
            "last_serviced_at": "2026-01-10",
        },
    )
    assert resp.status_code == 201, resp.text
    asset_id = resp.json()["id"]

    resp = await client.get("/api/admin/expiring-items", headers=auth_headers)
    assert resp.status_code == 200
    mirrored = [i for i in resp.json() if i["asset_id"] == asset_id]
    assert len(mirrored) == 1, "the asset did not surface on the expiry-items API"
    assert mirrored[0]["kind"] == "asset_service"
    assert mirrored[0]["expires_on"] == "2027-01-10"

    # A patch that doesn't MENTION the schedule must leave it alone. Without
    # `exclude_unset=True` in the handler, the unset `service_interval_days`
    # would serialise as null and silently switch the reminder off.
    resp = await client.put(
        f"/api/admin/assets/{asset_id}", headers=auth_headers, json={"location": "cellar"}
    )
    assert resp.status_code == 200, resp.text
    resp = await client.get("/api/admin/expiring-items", headers=auth_headers)
    assert [i for i in resp.json() if i["asset_id"] == asset_id] != [], (
        "an unrelated patch deleted the service reminder"
    )

    # An EXPLICIT null on the interval switches the reminder off.
    resp = await client.put(
        f"/api/admin/assets/{asset_id}", headers=auth_headers, json={"service_interval_days": None}
    )
    assert resp.status_code == 200
    resp = await client.get("/api/admin/expiring-items", headers=auth_headers)
    assert [i for i in resp.json() if i["asset_id"] == asset_id] == []


async def test_deleting_an_asset_does_not_500_the_page(client, auth_headers, db_pool):
    """A hand-curated expiring item pointing at the asset must not turn its
    deletion into an unhandled ForeignKeyViolation (HTTP 500) — the FK is
    ON DELETE SET NULL for exactly this reason."""
    resp = await client.post(
        "/api/admin/assets", headers=auth_headers, json={"name": f"{PREFIX}Sold Car", "kind": "car"}
    )
    asset_id = resp.json()["id"]
    resp = await client.post(
        "/api/admin/expiring-items",
        headers=auth_headers,
        json={
            "kind": "insurance",
            "title": f"{PREFIX}car insurance",
            "expires_on": "2030-01-01",
            "asset_id": asset_id,
        },
    )
    assert resp.status_code == 201, resp.text
    item_id = resp.json()["id"]

    resp = await client.delete(f"/api/admin/assets/{asset_id}", headers=auth_headers)
    assert resp.status_code == 204, resp.text

    resp = await client.get(f"/api/admin/expiring-items/{item_id}", headers=auth_headers)
    assert resp.status_code == 200, "the document was deleted along with its asset"
    assert resp.json()["asset_id"] is None


async def test_expiry_page_400s_on_an_asset_id_that_does_not_exist(client, auth_headers):
    """Migration 019 turned life.expiring_items.asset_id into a real foreign
    key, which makes a bogus asset_id on the ALREADY-SHIPPED Expiry page raise
    asyncpg.ForeignKeyViolationError. Unmapped, that is an HTTP 500."""
    for payload_extra in ({"asset_id": "00000000-0000-0000-0000-0000000000c7"},
                          {"person_id": "00000000-0000-0000-0000-0000000000c7"}):
        resp = await client.post(
            "/api/admin/expiring-items",
            headers=auth_headers,
            json={
                "kind": "warranty",
                "title": f"{PREFIX}dangling",
                "expires_on": "2030-01-01",
                **payload_extra,
            },
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == (
            "person_id or asset_id does not reference an existing row"
        )

    # An UPDATE that introduces the dangling link is mapped the same way.
    resp = await client.post(
        "/api/admin/expiring-items",
        headers=auth_headers,
        json={"kind": "warranty", "title": f"{PREFIX}real", "expires_on": "2030-01-01"},
    )
    assert resp.status_code == 201, resp.text
    resp = await client.put(
        f"/api/admin/expiring-items/{resp.json()['id']}",
        headers=auth_headers,
        json={"asset_id": "00000000-0000-0000-0000-0000000000c7"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "person_id or asset_id does not reference an existing row"


async def test_missing_asset_404s_with_the_handlers_message(client, auth_headers):
    missing = "00000000-0000-0000-0000-0000000000c7"
    for method in ("get", "delete"):
        resp = await getattr(client, method)(
            f"/api/admin/assets/{missing}", headers=auth_headers
        )
        assert resp.status_code == 404
        # Assert the HANDLER's detail, not a bare 404 — FastAPI 404s an
        # unknown path too, so a status-only assertion would still pass with
        # the route deleted.
        assert resp.json()["detail"] == "Asset not found"

    resp = await client.put(
        f"/api/admin/assets/{missing}", headers=auth_headers, json={"location": "ghost"}
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Asset not found"


async def test_create_validation_errors(client, auth_headers):
    # Pydantic rejects a missing required field outright...
    resp = await client.post("/api/admin/assets", headers=auth_headers, json={"name": "x"})
    assert resp.status_code == 422
    # ...and a whitespace-only name is caught by the service, surfaced as 400.
    resp = await client.post(
        "/api/admin/assets", headers=auth_headers, json={"name": "   ", "kind": "car"}
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "name is required"


async def test_mutations_are_audit_logged(client, auth_headers, db_pool):
    resp = await client.post(
        "/api/admin/assets",
        headers=auth_headers,
        json={"name": f"{PREFIX}Audited Asset", "kind": "tool"},
    )
    assert resp.status_code == 201
    asset_id = resp.json()["id"]

    row = await db_pool.fetchrow(
        "SELECT actor, action, target_type, details FROM audit_log "
        "WHERE target_id = $1 AND action = 'asset_created'",
        asset_id,
    )
    assert row is not None, "creating an asset wrote no audit_log row"
    assert row["actor"] == "api:assets_admin"
    assert row["target_type"] == "asset"
    assert row["details"] == {"slug": f"{PREFIX}audited-asset", "kind": "tool"}

    resp = await client.delete(f"/api/admin/assets/{asset_id}", headers=auth_headers)
    assert resp.status_code == 204
    assert await db_pool.fetchval(
        "SELECT 1 FROM audit_log WHERE target_id = $1 AND action = 'asset_deleted'",
        asset_id,
    )
