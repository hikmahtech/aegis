"""/api/admin/expiring-items — admin CRUD over the expiry registry (migration 018).

httpx ASGITransport + a REAL db_pool (TestClient would drive the app on a
second event loop and blow up with asyncpg "another operation is in
progress" the moment a handler touches the database).
"""

from __future__ import annotations

import base64
import re
from datetime import timedelta
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.db import run_migrations
from httpx import ASGITransport, AsyncClient

PREFIX = "zzexproute-"


async def _wipe(pool: asyncpg.Pool) -> None:
    # Children before parents: alerts -> items -> people/assets.
    await pool.execute(
        "DELETE FROM life.expiring_item_alerts WHERE item_id IN "
        "(SELECT id FROM life.expiring_items WHERE title LIKE $1)",
        f"{PREFIX}%",
    )
    await pool.execute("DELETE FROM life.expiring_items WHERE title LIKE $1", f"{PREFIX}%")
    await pool.execute("DELETE FROM life.people WHERE name LIKE $1", f"{PREFIX}%")
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


_PAGE = Path(__file__).resolve().parents[2] / "admin-panel/frontend/src/pages/ExpiringItems.tsx"


def _spa_save_payload(form: dict[str, str]) -> dict:
    """The body `ExpiringItems.tsx#handleSave` builds from its form state.

    Mirrored rather than invented: #200 is about the SPA's "empty the box"
    gesture reaching the service as an explicit null, so a hand-rolled request
    would prove the service works while the page stayed unable to drive it.
    `test_admin_panel_unlinks_with_an_explicit_null` pins this mirror to the
    real source so the two cannot drift apart in silence.
    """
    lead_days = []
    for chunk in form["lead_days"].split(","):
        try:
            lead_days.append(int(chunk.strip()))
        except ValueError:
            continue
    return {
        "kind": form["kind"].strip(),
        "title": form["title"].strip(),
        "expires_on": form["expires_on"],
        "lead_days": lead_days,
        "person_id": form["person_id"].strip() or None,
        "notes": form["notes"],
    }


async def test_admin_panel_unlinks_with_an_explicit_null():
    """The page must SEND `person_id: null` when its box is empty.

    If it ever switched to omitting the key, the handler's `exclude_unset=True`
    would drop it and the unlink would silently do nothing — the precise drift
    #200 describes. Read out of the source instead of restated here, which
    would only agree with itself.
    """
    src = _PAGE.read_text()
    assert re.search(r"person_id:\s*form\.person_id\.trim\(\)\s*\|\|\s*null", src), (
        "ExpiringItems.tsx no longer sends person_id as an explicit null — "
        "_spa_save_payload and the unlink path below are now fiction"
    )
    # ...and it offers a control that empties the box (the Unlink button).
    # Matched on the setForm call, not on the bare `person_id: ''`, which the
    # page's `emptyForm` constant satisfies whether or not the button exists.
    assert re.search(r"setForm\(\{\s*\.\.\.form,\s*person_id:\s*''\s*\}\)", src), (
        "ExpiringItems.tsx has no control that clears an existing person link"
    )


async def test_person_and_asset_links_can_be_cleared_through_the_route(
    client, auth_headers, db_pool, today
):
    person_id = await db_pool.fetchval(
        "INSERT INTO life.people (name) VALUES ($1) RETURNING id", f"{PREFIX}Owner"
    )
    asset_id = await db_pool.fetchval(
        "INSERT INTO life.assets (slug, name, kind) VALUES ($1, $2, 'appliance') RETURNING id",
        f"{PREFIX}boiler",
        f"{PREFIX}Boiler",
    )
    form = {
        "kind": "passport",
        "title": f"{PREFIX}Mislinked passport",
        "expires_on": (today + timedelta(days=200)).isoformat(),
        "lead_days": "30, 7, 1",
        "person_id": str(person_id),
        "notes": "linked to the wrong person",
    }
    payload = _spa_save_payload(form)
    payload["asset_id"] = str(asset_id)  # the page has no asset box; API-only link
    resp = await client.post("/api/admin/expiring-items", headers=auth_headers, json=payload)
    assert resp.status_code == 201, resp.text
    item_id = resp.json()["id"]
    assert resp.json()["person_id"] == str(person_id)
    assert resp.json()["asset_id"] == str(asset_id)

    # A patch that never mentions person_id leaves the link alone. Proven on
    # its own, because "the link survived" and "the link was cleared" are
    # decided by the same filter — a test showing only the clear would pass
    # just as happily if that filter wiped every unmentioned field.
    resp = await client.put(
        f"/api/admin/expiring-items/{item_id}",
        headers=auth_headers,
        json={"notes": "still linked"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["person_id"] == str(person_id)
    assert resp.json()["asset_id"] == str(asset_id)

    # Emptying the box in the edit modal detaches the person.
    form["person_id"] = "   "  # what `.trim() || null` turns into a JSON null
    resp = await client.put(
        f"/api/admin/expiring-items/{item_id}",
        headers=auth_headers,
        json=_spa_save_payload(form),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["person_id"] is None, "emptying the person box did not unlink"
    assert (
        await db_pool.fetchval(
            "SELECT person_id FROM life.expiring_items WHERE id = $1::uuid", item_id
        )
        is None
    ), "the response said unlinked but the column still holds the person"
    # The clear is targeted: a field the payload never mentioned is untouched,
    # and the rest of the same patch still applied.
    assert body["asset_id"] == str(asset_id)
    assert body["lead_days"] == [30, 7, 1]
    # Unlinking is not deleting — the person row is still there.
    assert (
        await db_pool.fetchval("SELECT name FROM life.people WHERE id = $1", person_id)
        == f"{PREFIX}Owner"
    )

    # The asset link detaches the same way, and the asset survives.
    resp = await client.put(
        f"/api/admin/expiring-items/{item_id}", headers=auth_headers, json={"asset_id": None}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["asset_id"] is None
    assert (
        await db_pool.fetchval(
            "SELECT asset_id FROM life.expiring_items WHERE id = $1::uuid", item_id
        )
        is None
    )
    assert await db_pool.fetchval("SELECT 1 FROM life.assets WHERE id = $1", asset_id) == 1
