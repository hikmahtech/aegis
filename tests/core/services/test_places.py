"""B5 — place inference and what a location push is allowed to persist.

Two halves, and the second is the deliverable:

* inference is right (inside/outside a radius, overlapping radii),
* and the coordinate is *gone* — absent from the stored row, from the
  `settings` pointer and from every log line the push emits.

Real Postgres: places live in `channels` rows and dedup is migration 021's
unique index, so a fake pool would test neither.

Isolation: every row is namespaced `zzb5-`. `life.observations.source` is the
constant "location" for this feature, so deletes scope on `external_id`, which
IS prefixed — scoping on source would delete a co-located file's rows.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from aegis.db import run_migrations
from aegis.services import places as svc
from structlog.testing import capture_logs

PREFIX = "zzb5-"
HOME = f"{PREFIX}home"
OFFICE = f"{PREFIX}office"
CITY = f"{PREFIX}city"

# An arbitrary fixed centre. Any point works; using a constant keeps the
# offsets below readable.
LAT = 19.0760
LON = 72.8777

# ~111.32 km per degree of latitude at any longitude, so these are exact-ish
# metre offsets due north.
_DEG_PER_M = 1.0 / 111_320.0


def _north(metres: float) -> float:
    return LAT + metres * _DEG_PER_M


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    await run_migrations(db_pool)
    await _cleanup(db_pool)
    yield db_pool
    await _cleanup(db_pool)


async def _cleanup(pool) -> None:
    await pool.execute(
        "DELETE FROM life.observations WHERE external_id LIKE $1", f"{PREFIX}%"
    )
    await pool.execute(
        "DELETE FROM channels WHERE kind = $1 AND identifier LIKE $2",
        svc.PLACE_CHANNEL_KIND,
        f"{PREFIX}%",
    )
    await pool.execute(
        "DELETE FROM settings WHERE key = $1", svc.CURRENT_PLACE_KEY
    )


async def _add_place(pool, name: str, lat: float, lon: float, radius_m, active=True):
    config: dict = {"lat": lat, "lon": lon}
    if radius_m is not None:
        config["radius_m"] = radius_m
    await pool.execute(
        "INSERT INTO channels (kind, identifier, config, active) VALUES ($1,$2,$3,$4)",
        svc.PLACE_CHANNEL_KIND,
        name,
        config,
        active,
    )


async def _count(pool) -> int:
    return await pool.fetchval(
        "SELECT count(*) FROM life.observations WHERE external_id LIKE $1", f"{PREFIX}%"
    )


# ---------------------------------------------------------------------------
# Distance + inference.
# ---------------------------------------------------------------------------


def test_haversine_returns_metres_not_degrees_or_kilometres():
    """One degree of latitude is ~111.32 km. Pinned with a literal, because a
    units slip here silently makes every place match (or none)."""
    metres = svc.haversine_m(0.0, 0.0, 1.0, 0.0)
    assert 111_000 < metres < 111_700, metres
    # ...and the same distance east on the equator.
    assert 111_000 < svc.haversine_m(0.0, 0.0, 0.0, 1.0) < 111_700


async def test_point_inside_a_place_radius_resolves_to_that_place(pool):
    await _add_place(pool, HOME, LAT, LON, 200)
    places = await svc.list_places(pool)
    assert svc.resolve_place(places, _north(50), LON) == HOME


async def test_point_ten_km_away_resolves_to_elsewhere(pool):
    await _add_place(pool, HOME, LAT, LON, 200)
    places = await svc.list_places(pool)
    assert svc.resolve_place(places, _north(10_000), LON) == "elsewhere"


async def test_point_just_outside_the_radius_resolves_to_elsewhere(pool):
    """The boundary, not just the far field — an off-by-1000 in the units
    would pass the 10 km test above but fail here."""
    await _add_place(pool, HOME, LAT, LON, 200)
    places = await svc.list_places(pool)
    assert svc.resolve_place(places, _north(250), LON) == "elsewhere"


def test_overlapping_radii_resolve_to_the_tighter_place():
    """A small office circle inside a 2 km city circle must win — in EITHER
    row order, since `list_places` has no ORDER BY and Postgres may hand them
    back either way."""
    city = {"name": CITY, "lat": LAT, "lon": LON, "radius_m": 2000.0}
    office = {"name": OFFICE, "lat": LAT, "lon": LON, "radius_m": 150.0}
    assert svc.resolve_place([city, office], LAT, LON) == OFFICE
    assert svc.resolve_place([office, city], LAT, LON) == OFFICE


async def test_a_place_row_missing_coordinates_is_skipped_not_fatal(pool):
    """One fat-fingered place must not stop every other push resolving."""
    await pool.execute(
        "INSERT INTO channels (kind, identifier, config, active) VALUES ($1,$2,$3,true)",
        svc.PLACE_CHANNEL_KIND,
        f"{PREFIX}broken",
        {"radius_m": 100},
    )
    await _add_place(pool, HOME, LAT, LON, 200)
    places = await svc.list_places(pool)
    assert [p["name"] for p in places] == [HOME]
    assert svc.resolve_place(places, LAT, LON) == HOME


async def test_inactive_place_rows_are_not_matched(pool):
    await _add_place(pool, HOME, LAT, LON, 200, active=False)
    places = await svc.list_places(pool)
    assert places == []
    assert svc.resolve_place(places, LAT, LON) == "elsewhere"


async def test_place_row_without_radius_uses_the_default(pool):
    await _add_place(pool, HOME, LAT, LON, None)
    places = await svc.list_places(pool)
    assert places[0]["radius_m"] == 150.0
    assert svc.resolve_place(places, _north(100), LON) == HOME
    assert svc.resolve_place(places, _north(400), LON) == "elsewhere"


# ---------------------------------------------------------------------------
# Payload parsing.
# ---------------------------------------------------------------------------


def test_parse_fix_accepts_both_client_spellings():
    assert svc.parse_fix({"lat": 1.5, "lon": 2.5}) == (1.5, 2.5)
    assert svc.parse_fix({"latitude": 1.5, "longitude": 2.5}) == (1.5, 2.5)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"lat": 1.0},
        {"lon": 1.0},
        {"lat": "north", "lon": 2.0},
        {"lat": 91.0, "lon": 0.0},
        {"lat": 0.0, "lon": 181.0},
        {"lat": None, "lon": None},
    ],
)
def test_parse_fix_rejects_anything_that_is_not_a_position(payload):
    with pytest.raises(ValueError):
        svc.parse_fix(payload)


def test_device_timestamp_is_used_when_plausible():
    now = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    taken = datetime(2026, 7, 29, 8, 15, tzinfo=UTC)
    got = svc.fix_observed_at({"tst": int(taken.timestamp())}, now=now)
    assert got == taken


@pytest.mark.parametrize("tst", [0, -1, 1, 99_999_999_999_999, "soon", None, True])
def test_implausible_device_timestamp_falls_back_to_arrival(tst):
    """A dead clock, a millisecond stamp or no stamp must not park the row
    outside every trend query (and outside retention) for a year."""
    now = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    assert svc.fix_observed_at({"tst": tst}, now=now) == now


# ---------------------------------------------------------------------------
# What is persisted — the privacy contract.
# ---------------------------------------------------------------------------


async def test_push_stores_the_label_and_not_the_coordinate(pool):
    await _add_place(pool, HOME, LAT, LON, 200)
    fix_lat, fix_lon = _north(50), LON
    row = await svc.record_location_push(
        pool,
        {"lat": fix_lat, "lon": fix_lon, "acc": 12, "t": "p", "tid": "AB"},
        f"{PREFIX}evt-1",
    )
    assert row is not None
    assert row["source"] == "location"
    assert row["metric"] == "place"
    assert row["value"] is None
    assert row["metadata"] == {"place": HOME, "trigger": "p"}

    # Nothing anywhere in the stored row resembles the fix — not the row, not
    # the pointer. Checked over the serialised forms so a future extra column
    # or metadata key cannot smuggle one back in.
    stored = await pool.fetchrow(
        "SELECT * FROM life.observations WHERE external_id = $1", f"{PREFIX}evt-1"
    )
    pointer = await pool.fetchval(
        "SELECT value FROM settings WHERE key = $1", svc.CURRENT_PLACE_KEY
    )
    blob = json.dumps(dict(stored), default=str) + json.dumps(pointer, default=str)
    for needle in (f"{fix_lat:.4f}", f"{fix_lon:.4f}", "lat", "lon", "acc"):
        assert needle not in blob, f"{needle!r} leaked into storage: {blob}"


async def test_push_outside_every_place_stores_elsewhere(pool):
    await _add_place(pool, HOME, LAT, LON, 200)
    row = await svc.record_location_push(
        pool, {"lat": _north(9_000), "lon": LON}, f"{PREFIX}evt-far"
    )
    assert row["metadata"]["place"] == "elsewhere"


async def test_replaying_the_same_external_id_writes_no_second_row(pool):
    await _add_place(pool, HOME, LAT, LON, 200)
    payload = {"lat": _north(10), "lon": LON}
    assert await svc.record_location_push(pool, payload, f"{PREFIX}evt-2") is not None
    assert await svc.record_location_push(pool, payload, f"{PREFIX}evt-2") is None
    assert await _count(pool) == 1


async def test_a_malformed_push_stores_nothing(pool):
    with pytest.raises(ValueError):
        await svc.record_location_push(pool, {"acc": 10}, f"{PREFIX}evt-bad")
    assert await _count(pool) == 0


async def test_the_fix_never_reaches_a_log_line(pool):
    """Structured logs go to disk and to OTel; a coordinate in one is the same
    leak as a coordinate in the database.

    `structlog.testing.capture_logs` — caplog would see nothing here, since
    structlog does not route through stdlib logging in this codebase.
    """
    await _add_place(pool, HOME, LAT, LON, 200)
    fix_lat, fix_lon = _north(50), LON
    with capture_logs() as captured:
        await svc.record_location_push(
            pool, {"lat": fix_lat, "lon": fix_lon}, f"{PREFIX}evt-3"
        )
    blob = json.dumps(captured, default=str)
    assert f"{fix_lat:.4f}" not in blob, blob
    assert f"{fix_lon:.4f}" not in blob, blob
    # ...and the place name is not logged either: a log stream of place names
    # is a presence timeline by another route.
    assert HOME not in blob, blob
    assert any(e.get("event") == "location_push_recorded" for e in captured), captured
    assert any(e.get("place_matched") is True for e in captured), captured


# ---------------------------------------------------------------------------
# The `current_place` pointer.
# ---------------------------------------------------------------------------


async def test_current_place_tracks_the_latest_fix(pool):
    await _add_place(pool, HOME, LAT, LON, 200)
    await _add_place(pool, OFFICE, _north(5_000), LON, 200)
    await svc.record_location_push(pool, {"lat": LAT, "lon": LON}, f"{PREFIX}p1")
    await svc.record_location_push(
        pool, {"lat": _north(5_000), "lon": LON}, f"{PREFIX}p2"
    )
    pointer = await pool.fetchval(
        "SELECT value FROM settings WHERE key = $1", svc.CURRENT_PLACE_KEY
    )
    assert pointer["place"] == OFFICE


async def test_a_late_arriving_older_fix_does_not_move_current_place(pool):
    """A phone that was offline replays its queue out of order. Letting an old
    push win would have the briefing announce a place already left."""
    await _add_place(pool, HOME, LAT, LON, 200)
    await _add_place(pool, OFFICE, _north(5_000), LON, 200)
    now = datetime.now(UTC)
    await svc.record_location_push(
        pool,
        {"lat": _north(5_000), "lon": LON, "tst": int(now.timestamp())},
        f"{PREFIX}p3",
    )
    await svc.record_location_push(
        pool,
        {"lat": LAT, "lon": LON, "tst": int((now - timedelta(hours=4)).timestamp())},
        f"{PREFIX}p4",
    )
    pointer = await pool.fetchval(
        "SELECT value FROM settings WHERE key = $1", svc.CURRENT_PLACE_KEY
    )
    assert pointer["place"] == OFFICE
    # The older push is still stored as an observation — only the "where are
    # you now" pointer refuses to rewind.
    assert await _count(pool) == 2
