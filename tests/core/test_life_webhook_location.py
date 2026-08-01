"""B5 — the `location` lane of POST /api/webhooks/life/{source}.

B4's file already pins the signature/replay/body-bound machinery, which is
source-agnostic. What is new here is what the location lane *does* with an
authenticated push: resolve it to a configured place, store the label, and
drop the coordinate.

Real Postgres, httpx ASGITransport (TestClient's separate event loop would
InterfaceError on the shared pool).

Isolation: places and idempotency claims are namespaced `zzb5w-`;
`life.observations.source` is the constant "location" for this feature, so
deletes scope on the prefixed `external_id`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.api.routes.interactions import get_workflow_client
from aegis.config import Settings
from aegis.services.places import CURRENT_PLACE_KEY, PLACE_CHANNEL_KIND
from httpx import ASGITransport, AsyncClient

SECRET = "zzb5w-test-life-secret"
SOURCE = "location"
PREFIX = "zzb5w-"
HOME = f"{PREFIX}home"

LAT = 19.0760
LON = 72.8777
_DEG_PER_M = 1.0 / 111_320.0

_TEST_SETTINGS = {
    "database_url": "postgresql://test:test@localhost:5432/test",
    "litellm_url": "https://litellm.example.com/v1",
    "temporal_ui_url": "https://temporal.example.com",
    "n8n_ui_url": "https://n8n.example.com",
    "admin_username": "admin",
    "admin_password": "admin",
    "life_webhook_secret": SECRET,
}


def _push(body: dict) -> tuple[str, bytes, dict[str, str]]:
    """(url, raw body, headers) signed with B4's literal wire scheme."""
    raw = json.dumps(body).encode()
    stamp = str(int(time.time()))
    sig = hmac.new(
        SECRET.encode(), f"{SOURCE}.{stamp}.".encode() + raw, hashlib.sha256
    ).hexdigest()
    return (
        f"/api/webhooks/life/{SOURCE}",
        raw,
        {"X-Aegis-Timestamp": stamp, "X-Aegis-Signature": sig},
    )


async def _cleanup(pool) -> None:
    await pool.execute(
        "DELETE FROM life.observations WHERE external_id LIKE $1", f"{PREFIX}%"
    )
    await pool.execute(
        "DELETE FROM ingest_idempotency WHERE external_id LIKE $1", f"{PREFIX}%"
    )
    await pool.execute("DELETE FROM audit_log WHERE target_id LIKE $1", f"{PREFIX}%")
    await pool.execute(
        "DELETE FROM channels WHERE kind = $1 AND identifier LIKE $2",
        PLACE_CHANNEL_KIND,
        f"{PREFIX}%",
    )
    await pool.execute("DELETE FROM settings WHERE key = $1", CURRENT_PLACE_KEY)


@pytest_asyncio.fixture(loop_scope="function")
async def client(db_pool):
    await _cleanup(db_pool)
    await db_pool.execute(
        "INSERT INTO channels (kind, identifier, config, active) VALUES ($1,$2,$3,true)",
        PLACE_CHANNEL_KIND,
        HOME,
        {"lat": LAT, "lon": LON, "radius_m": 200},
    )
    app = create_app(run_lifespan=False)
    app.state.db_pool = db_pool
    app.dependency_overrides[get_settings] = lambda: Settings(**_TEST_SETTINGS)
    temporal = MagicMock()
    temporal.start_workflow = AsyncMock()
    app.dependency_overrides[get_workflow_client] = lambda: temporal
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.temporal = temporal
        yield c
    await _cleanup(db_pool)


async def _rows(pool) -> list[dict]:
    rows = await pool.fetch(
        "SELECT source, metric, value, metadata FROM life.observations "
        "WHERE external_id LIKE $1",
        f"{PREFIX}%",
    )
    return [dict(r) for r in rows]


async def test_signed_location_push_stores_one_place_label(client, db_pool):
    fix_lat = LAT + 50 * _DEG_PER_M
    url, raw, headers = _push(
        {"id": f"{PREFIX}evt-1", "_type": "location", "lat": fix_lat, "lon": LON,
         "acc": 8, "t": "p"}
    )
    resp = await client.post(url, content=raw, headers=headers)
    assert resp.status_code == 202, resp.text
    assert resp.json()["accepted"] is True

    rows = await _rows(db_pool)
    assert len(rows) == 1
    assert rows[0]["source"] == "location"
    assert rows[0]["metric"] == "place"
    assert rows[0]["metadata"] == {"place": HOME, "trigger": "p"}
    # The lane is inline: no durable workflow is started for a location ping.
    client.temporal.start_workflow.assert_not_awaited()
    # And the pointer the briefing reads holds a label, not a position.
    pointer = await db_pool.fetchval(
        "SELECT value FROM settings WHERE key = $1", CURRENT_PLACE_KEY
    )
    assert pointer["place"] == HOME
    assert "lat" not in json.dumps(pointer)


async def test_push_outside_every_configured_place_is_elsewhere(client, db_pool):
    url, raw, headers = _push(
        {"id": f"{PREFIX}evt-far", "lat": LAT + 20_000 * _DEG_PER_M, "lon": LON}
    )
    assert (await client.post(url, content=raw, headers=headers)).status_code == 202
    rows = await _rows(db_pool)
    assert [r["metadata"]["place"] for r in rows] == ["elsewhere"]


async def test_replayed_location_push_writes_no_second_row(client, db_pool):
    url, raw, headers = _push({"id": f"{PREFIX}evt-2", "lat": LAT, "lon": LON})
    first = await client.post(url, content=raw, headers=headers)
    second = await client.post(url, content=raw, headers=headers)
    assert first.status_code == 202 and second.status_code == 202
    assert second.json()["duplicate"] is True
    assert len(await _rows(db_pool)) == 1


async def test_push_without_a_fix_is_422_and_releases_its_claim(client, db_pool):
    """A client-fixable payload must not burn the device's `id` forever."""
    bad = _push({"id": f"{PREFIX}evt-3", "acc": 10})
    resp = await client.post(bad[0], content=bad[1], headers=bad[2])
    assert resp.status_code == 422
    assert resp.json()["detail"] == "invalid_observation"
    assert await _rows(db_pool) == []

    good = _push({"id": f"{PREFIX}evt-3", "lat": LAT, "lon": LON})
    retry = await client.post(good[0], content=good[1], headers=good[2])
    assert retry.status_code == 202
    assert retry.json().get("duplicate") is None
    assert len(await _rows(db_pool)) == 1


async def test_the_response_never_echoes_the_coordinate(client):
    """The 202 body is what a proxy log and a phone's shortcut history keep."""
    fix_lat, fix_lon = LAT + 30 * _DEG_PER_M, LON
    url, raw, headers = _push({"id": f"{PREFIX}evt-4", "lat": fix_lat, "lon": fix_lon})
    resp = await client.post(url, content=raw, headers=headers)
    assert resp.status_code == 202
    body = resp.text
    assert f"{fix_lat:.4f}" not in body
    assert f"{fix_lon:.4f}" not in body
