"""Route tests for the /api/admin/channels CRUD API (email/rss/raindrop)."""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from aegis.api.auth import verify_auth
from aegis.api.routes import channels
from fastapi import FastAPI

pytestmark = pytest.mark.asyncio

_TEST_PREFIX = "chan-route-test-"


@pytest_asyncio.fixture(loop_scope="function")
async def client(db_pool):
    app = FastAPI()
    app.include_router(channels.router)
    app.dependency_overrides[verify_auth] = lambda: True
    app.state.db_pool = db_pool
    await db_pool.execute("DELETE FROM channels WHERE identifier LIKE $1", f"{_TEST_PREFIX}%")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c
    await db_pool.execute("DELETE FROM channels WHERE identifier LIKE $1", f"{_TEST_PREFIX}%")


async def _create(client, **overrides) -> dict:
    payload = {
        "kind": "rss",
        "identifier": f"{_TEST_PREFIX}{uuid4().hex[:8]}",
        "config": {"label": "test-feed", "agent_id": "raphael"},
        "active": True,
    }
    payload.update(overrides)
    r = await client.post("/api/admin/channels", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


async def test_create_and_list(client):
    created = await _create(client, config={"label": "hn", "agent_id": "raphael"})
    assert created["kind"] == "rss"
    assert created["config"] == {"label": "hn", "agent_id": "raphael"}
    assert created["active"] is True

    r = await client.get("/api/admin/channels")
    assert r.status_code == 200
    listed = [c for c in r.json() if c["id"] == created["id"]]
    assert len(listed) == 1
    assert listed[0]["identifier"] == created["identifier"]

    # kind filter includes it; a different kind filter excludes it
    rss = (await client.get("/api/admin/channels?kind=rss")).json()
    assert any(c["id"] == created["id"] for c in rss)
    email = (await client.get("/api/admin/channels?kind=email")).json()
    assert not any(c["id"] == created["id"] for c in email)


async def test_create_rejects_unknown_kind(client):
    r = await client.post(
        "/api/admin/channels",
        json={"kind": "telegram", "identifier": f"{_TEST_PREFIX}bad-kind"},
    )
    assert r.status_code == 400
    assert "telegram" in r.json()["detail"]


async def test_create_rejects_empty_identifier(client):
    r = await client.post("/api/admin/channels", json={"kind": "rss", "identifier": "   "})
    assert r.status_code == 400


async def test_create_duplicate_identifier_conflicts(client):
    created = await _create(client)
    r = await client.post(
        "/api/admin/channels",
        json={"kind": "rss", "identifier": created["identifier"]},
    )
    assert r.status_code == 409
    # Same identifier under a DIFFERENT kind is fine (unique is per (kind, identifier)).
    r2 = await client.post(
        "/api/admin/channels",
        json={"kind": "email", "identifier": created["identifier"]},
    )
    assert r2.status_code == 201


async def test_patch_config_active_and_identifier(client):
    created = await _create(client, active=True)
    new_identifier = f"{_TEST_PREFIX}renamed-{uuid4().hex[:8]}"
    r = await client.patch(
        f"/api/admin/channels/{created['id']}",
        json={
            "identifier": new_identifier,
            "config": {"label": "edited", "agent_id": "sebas"},
            "active": False,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["identifier"] == new_identifier
    assert body["config"] == {"label": "edited", "agent_id": "sebas"}
    assert body["active"] is False

    # the change persisted
    listed = (await client.get("/api/admin/channels?kind=rss")).json()
    row = next(c for c in listed if c["id"] == created["id"])
    assert row["identifier"] == new_identifier
    assert row["active"] is False


async def test_patch_partial_leaves_other_fields(client):
    created = await _create(client, config={"label": "keep-me"}, active=True)
    r = await client.patch(f"/api/admin/channels/{created['id']}", json={"active": False})
    assert r.status_code == 200
    body = r.json()
    assert body["active"] is False
    assert body["identifier"] == created["identifier"]
    assert body["config"] == {"label": "keep-me"}


async def test_patch_identifier_conflict_409(client):
    a = await _create(client)
    b = await _create(client)
    r = await client.patch(
        f"/api/admin/channels/{b['id']}", json={"identifier": a["identifier"]}
    )
    assert r.status_code == 409


async def test_patch_empty_body_400(client):
    created = await _create(client)
    r = await client.patch(f"/api/admin/channels/{created['id']}", json={})
    assert r.status_code == 400


async def test_patch_unknown_id_404(client):
    r = await client.patch(f"/api/admin/channels/{uuid4()}", json={"active": True})
    assert r.status_code == 404


async def test_delete_channel(client):
    created = await _create(client)
    r = await client.delete(f"/api/admin/channels/{created['id']}")
    assert r.status_code == 204
    listed = (await client.get("/api/admin/channels")).json()
    assert not any(c["id"] == created["id"] for c in listed)
    # deleting again → 404
    r2 = await client.delete(f"/api/admin/channels/{created['id']}")
    assert r2.status_code == 404


async def test_place_channel_round_trips_with_its_coordinates(client):
    """B5 — named places are `channels` rows, so the kind must be accepted and
    its {lat, lon, radius_m} config must survive the round trip. Without the
    kind in CHANNEL_KINDS this is a 400 and location pushes can never resolve
    to anything but "elsewhere"."""
    created = await _create(
        client,
        kind="place",
        config={"lat": 19.076, "lon": 72.8777, "radius_m": 200, "label": "flat"},
    )
    assert created["kind"] == "place"
    listed = (await client.get("/api/admin/channels?kind=place")).json()
    fetched = next(c for c in listed if c["id"] == created["id"])
    assert fetched["config"]["lat"] == 19.076
    assert fetched["config"]["lon"] == 72.8777
    assert fetched["config"]["radius_m"] == 200


async def test_admin_panel_renders_every_channel_kind():
    """`Channels.tsx` keeps its OWN copy of the kind list and that copy is what
    drives rendering, so a kind present only server-side produces rows nobody
    can see or edit in the admin panel (this bit B7).

    Parsed out of the source rather than duplicated here — a literal list in
    this test would agree with itself while the page stayed stale.
    """
    import re
    from pathlib import Path

    page = (
        Path(__file__).resolve().parents[2]
        / "admin-panel/frontend/src/pages/Channels.tsx"
    )
    match = re.search(r"const CHANNEL_KINDS = \[(.*?)\] as const;", page.read_text())
    assert match, "CHANNEL_KINDS not found in Channels.tsx"
    frontend = {k.strip().strip("'\"") for k in match.group(1).split(",") if k.strip()}
    assert set(channels.CHANNEL_KINDS) == frontend
