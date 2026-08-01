"""Named places and location pushes (B5).

PRIVACY MODEL — read this before changing anything in here.

A phone pushing its position every few minutes is the most sensitive stream
this system can carry, so this module is built to *throw the coordinates
away*:

* A push's ``lat``/``lon`` are used exactly once — to pick which configured
  place contains them — and are then discarded. Nothing writes them to
  ``life.observations``, to ``settings``, or to a log line. What is stored is
  a place LABEL (``"home"``, ``"office"``, ``"elsewhere"``): a word the owner
  chose, not a position.
* The only coordinates AEGIS persists are the handful of place centres the
  owner typed into the admin Channels page (``channels`` rows,
  ``kind='place'``, ``config={lat, lon, radius_m}``). Those are
  user-configured reference points, never inferred from traffic — a system
  that learned "home" from where the phone sleeps at night would be building
  exactly the dataset this module refuses to keep.
* Log lines carry ``place_matched`` (a boolean). Not the fix, and not the
  place name either: structured logs go to disk and to OTel, and a log stream
  of place names is a presence timeline by another route.

The consequence, taken deliberately: a place the owner never configured can
never be recovered afterwards, because the coordinate that would have matched
it is gone by the time the row is written. Re-deriving history is impossible
by construction, which is the point.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import structlog

from aegis.services.observations import record_external_observation

logger = structlog.get_logger()

# `channels.kind` for a named place. Reusing `channels` (rather than a new
# table) is what gives places admin CRUD, activate/deactivate and seeding for
# free — see routes/channels.py::CHANNEL_KINDS.
PLACE_CHANNEL_KIND = "place"

# The observation this module writes: one row per accepted push.
LOCATION_SOURCE = "location"
LOCATION_METRIC = "place"

# Resolved label when no configured place contains the fix. Deliberately a
# label and not `None`: "the owner was somewhere they have not named" is a
# real answer, and it keeps the series total.
UNKNOWN_PLACE = "elsewhere"

# settings KV holding {"place": str, "at": iso8601}. Read by the worker's
# briefing gather; holds a label and a timestamp, never a coordinate.
CURRENT_PLACE_KEY = "current_place"

# Radius assumed when a place row omits `radius_m`. ~a large building.
DEFAULT_RADIUS_M = 150.0

# Mean earth radius (IUGG), metres.
_EARTH_RADIUS_M = 6_371_008.8

# Oldest device-supplied `tst` we will believe. Anything before this is a
# broken clock, not a backfill, and would land the row outside every trend
# query while surviving retention for a year.
_MIN_OBSERVED_AT = datetime(2015, 1, 1, tzinfo=UTC)

# How far into the future a device clock may be and still be believed.
_MAX_CLOCK_SKEW = timedelta(days=1)

_LAT_KEYS = ("lat", "latitude")
_LON_KEYS = ("lon", "lng", "long", "longitude")


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two WGS84 points.

    Plain trigonometry on purpose: place radii are tens to hundreds of metres
    and haversine is accurate to well under a metre at that scale, so PostGIS
    (a server extension, a migration and a deployment dependency) buys nothing
    here.
    """
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = p2 - p1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def _first_number(payload: dict, keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if key not in payload:
            continue
        try:
            value = float(payload[key])
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None
    return None


def parse_fix(payload: dict) -> tuple[float, float]:
    """``(lat, lon)`` from an OwnTracks / Home-Assistant shaped payload.

    Accepts both spellings each client uses (``lat``/``lon`` for OwnTracks,
    ``latitude``/``longitude`` for Home Assistant). Raises ``ValueError`` on
    anything that is not a real position, which the webhook turns into a 422 —
    a malformed push is a client-fixable error, never a stored row.
    """
    lat = _first_number(payload, _LAT_KEYS)
    lon = _first_number(payload, _LON_KEYS)
    if lat is None or lon is None:
        raise ValueError("payload carries no usable lat/lon")
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        raise ValueError("lat/lon out of range")
    return lat, lon


def fix_observed_at(payload: dict, now: datetime | None = None) -> datetime:
    """When the fix was taken: the device's ``tst``, else arrival time.

    Honouring the device clock matters because ``life.observations`` is pruned
    by ``observed_at`` — a queued push replayed after a day offline must age
    out on its own schedule, not a day late. An implausible ``tst`` (dead
    clock, milliseconds instead of seconds) falls back to arrival time rather
    than being trusted.
    """
    current = now or datetime.now(UTC)
    try:
        stamp = datetime.fromtimestamp(int(payload.get("tst")), UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        # absent (int(None) → TypeError), non-numeric, or out of the platform's
        # representable range. `True` survives as 1 and is caught by the
        # plausibility window below, so it needs no special case.
        return current
    if _MIN_OBSERVED_AT <= stamp <= current + _MAX_CLOCK_SKEW:
        return stamp
    return current


async def list_places(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    """Active named places: ``[{name, lat, lon, radius_m}, ...]``.

    A row missing or mangling ``lat``/``lon`` is skipped with a warning rather
    than raising — one fat-fingered place must not stop every location push
    from being recorded.
    """
    rows = await pool.fetch(
        "SELECT identifier, config FROM channels WHERE kind = $1 AND active = true",
        PLACE_CHANNEL_KIND,
    )
    places: list[dict[str, Any]] = []
    for row in rows:
        config = row["config"]
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except ValueError:
                config = None
        if not isinstance(config, dict):
            config = {}
        lat = _first_number(config, ("lat",))
        lon = _first_number(config, ("lon",))
        if lat is None or lon is None or not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
            logger.warning("place_channel_misconfigured", place=row["identifier"])
            continue
        radius = _first_number(config, ("radius_m",))
        if radius is None or radius <= 0:
            radius = DEFAULT_RADIUS_M
        places.append(
            {"name": row["identifier"], "lat": lat, "lon": lon, "radius_m": radius}
        )
    return places


def resolve_place(places: list[dict[str, Any]], lat: float, lon: float) -> str:
    """Name of the tightest configured place containing the fix.

    Overlap is resolved by the SMALLEST radius, not by row order: an "office"
    circle sitting inside a 2 km "city-centre" circle must resolve to
    "office", and `list_places` returns rows in whatever order Postgres feels
    like. No containing place ⇒ :data:`UNKNOWN_PLACE`.
    """
    best: dict[str, Any] | None = None
    for place in places:
        if haversine_m(lat, lon, place["lat"], place["lon"]) > place["radius_m"]:
            continue
        if best is None or place["radius_m"] < best["radius_m"]:
            best = place
    return str(best["name"]) if best is not None else UNKNOWN_PLACE


async def set_current_place(pool: asyncpg.Pool, place: str, at: datetime) -> None:
    """Point ``settings.current_place`` at the newest fix seen so far.

    Guarded against going backwards: a push queued on a phone that was offline
    arrives after newer ones, and letting it win would have the briefing
    announce a place the owner left hours ago. `at` is normalised to whole
    seconds in UTC so the stored strings are fixed-width and compare
    chronologically as text — no cast that a hand-edited row could blow up.
    """
    stamp = at.astimezone(UTC).replace(microsecond=0).isoformat()
    await pool.execute(
        "INSERT INTO settings (key, value) VALUES ($1, $2) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now() "
        "WHERE settings.value->>'at' IS NULL "
        "OR settings.value->>'at' <= EXCLUDED.value->>'at'",
        CURRENT_PLACE_KEY,
        {"place": place, "at": stamp},
    )


async def record_location_push(
    pool: asyncpg.Pool, payload: dict, external_id: str
) -> dict | None:
    """Resolve one location push to a place and store the LABEL only.

    Returns the stored observation row, or ``None`` when ``external_id`` was
    already ingested. Raises ``ValueError`` for a payload that carries no
    usable fix.

    Dedup is `record_external_observation`'s partial unique index on
    ``(source, metric, external_id)`` — the webhook's `ingest_idempotency`
    claim already collapses an identical replay, and this is the second,
    database-level line that also holds if a claim is ever released and the
    push retried.
    """
    lat, lon = parse_fix(payload)
    places = await list_places(pool)
    place = resolve_place(places, lat, lon)
    observed_at = fix_observed_at(payload)
    del lat, lon  # nothing below this line may see the fix

    row = await record_external_observation(
        pool,
        source=LOCATION_SOURCE,
        metric=LOCATION_METRIC,
        external_id=external_id,
        value=None,
        observed_at=observed_at,
        # `trigger` is OwnTracks' one-character reason code (p=ping,
        # c=region, u=manual). Not a position, and it is what distinguishes
        # "arrived somewhere" from "still here".
        metadata={"place": place, "trigger": str(payload.get("t") or "")[:8]},
    )
    if row is None:
        logger.info("location_push_duplicate")
        return None
    await set_current_place(pool, place, observed_at)
    logger.info("location_push_recorded", place_matched=place != UNKNOWN_PLACE)
    return row
