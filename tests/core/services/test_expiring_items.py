"""services/expiring_items.py — CRUD + due_within over life.expiring_items (018).

Real Postgres (the session's freshly-migrated test database via `db_pool`);
no mocks, so the SQL, the `life` schema and the date arithmetic are all
exercised.

Every window assertion is anchored to the DATABASE's CURRENT_DATE, not
Python's `date.today()` — `due_within` compares against the server clock, and
the two disagree for part of every day whenever the test host is not UTC.
"""

from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from aegis.db import run_migrations
from aegis.services import expiring_items as svc

# Prefix every fixture row so the assertions can't be satisfied (or broken) by
# rows another test in the shared database left behind.
PREFIX = "zzexpiring-"


async def _wipe(pool: asyncpg.Pool) -> None:
    # Children before parents: alerts -> items -> people. Deleting people
    # first would be fine here (ON DELETE SET NULL) but the alert ledger is a
    # hard FK, so order matters for the item rows.
    await pool.execute(
        "DELETE FROM life.expiring_item_alerts WHERE item_id IN "
        "(SELECT id FROM life.expiring_items WHERE title LIKE $1)",
        f"{PREFIX}%",
    )
    await pool.execute("DELETE FROM life.expiring_items WHERE title LIKE $1", f"{PREFIX}%")
    await pool.execute("DELETE FROM life.people WHERE name LIKE $1", f"{PREFIX}%")


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    await run_migrations(db_pool)
    await _wipe(db_pool)
    yield db_pool
    await _wipe(db_pool)


@pytest_asyncio.fixture(loop_scope="function")
async def today(pool) -> date:
    """The database's idea of today — the clock `due_within` actually uses."""
    return await pool.fetchval("SELECT CURRENT_DATE")


# --------------------------------------------------------------------------
# normalize_lead_days
# --------------------------------------------------------------------------


def test_normalize_lead_days_dedupes_and_sorts_descending():
    # Descending is what C6 will walk (widest warning first); duplicates would
    # make the same threshold fire twice in one cycle.
    assert svc.normalize_lead_days([7, 30, 7, 1]) == [30, 7, 1]
    assert svc.normalize_lead_days(["30", 1, 7.0]) == [30, 7, 1]
    assert svc.normalize_lead_days(14) == [14]
    # 0 means "on the day" and is kept; negatives are past-due, which
    # due_within already surfaces, so they are dropped.
    assert svc.normalize_lead_days([0, -5, 3]) == [3, 0]


def test_normalize_lead_days_falls_back_to_the_default_ladder():
    assert svc.normalize_lead_days(None) == [30, 7, 1]
    assert svc.normalize_lead_days([]) == [30, 7, 1]
    # Nothing survives the filter -> the default, never an empty ladder (an
    # item with no thresholds would silently never warn).
    assert svc.normalize_lead_days(["nonsense", -1]) == [30, 7, 1]


# --------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------


async def test_create_get_update_delete_round_trip(pool, today):
    person_id = await pool.fetchval(
        "INSERT INTO life.people (name) VALUES ($1) RETURNING id", f"{PREFIX}Owner"
    )
    asset_id = uuid4()
    created = await svc.create_expiring_item(
        pool,
        {
            "kind": "  Passport  ",
            "title": f"{PREFIX}Indian passport",
            "expires_on": today + timedelta(days=200),
            "lead_days": [7, 90, 7],
            "person_id": person_id,
            "asset_id": asset_id,
            "notes": "renew at the Pune PSK",
            "metadata": {"source": "manual"},
        },
    )
    # kind is lowercased and stripped by the service.
    assert created["kind"] == "passport"
    assert created["title"] == f"{PREFIX}Indian passport"
    assert created["expires_on"] == today + timedelta(days=200)
    assert created["lead_days"] == [90, 7]
    assert created["person_id"] == person_id
    assert created["asset_id"] == asset_id
    assert created["notes"] == "renew at the Pune PSK"
    # jsonb columns come back as Python objects, not strings (pool codec).
    assert created["metadata"] == {"source": "manual"}
    assert created["created_at"] is not None

    fetched = await svc.get_expiring_item(pool, created["id"])
    assert fetched is not None
    assert fetched["id"] == created["id"]
    assert fetched["title"] == created["title"]

    listed = await svc.list_expiring_items(pool)
    assert created["id"] in [i["id"] for i in listed]

    updated = await svc.update_expiring_item(
        pool,
        created["id"],
        {"expires_on": today + timedelta(days=400), "notes": "renewed 2026"},
    )
    assert updated is not None
    assert updated["expires_on"] == today + timedelta(days=400)
    assert updated["notes"] == "renewed 2026"
    # Untouched fields survive a partial patch.
    assert updated["kind"] == "passport"
    assert updated["lead_days"] == [90, 7]

    assert await svc.delete_expiring_item(pool, created["id"]) is True
    assert await svc.get_expiring_item(pool, created["id"]) is None
    # Deleting an already-gone row is False, not an exception.
    assert await svc.delete_expiring_item(pool, created["id"]) is False


async def test_create_requires_kind_title_and_expiry(pool, today):
    with pytest.raises(ValueError, match="kind is required"):
        await svc.create_expiring_item(
            pool, {"kind": "   ", "title": f"{PREFIX}x", "expires_on": today}
        )
    with pytest.raises(ValueError, match="title is required"):
        await svc.create_expiring_item(
            pool, {"kind": "visa", "title": "   ", "expires_on": today}
        )
    with pytest.raises(ValueError, match="expires_on is required"):
        await svc.create_expiring_item(
            pool, {"kind": "visa", "title": f"{PREFIX}no date", "expires_on": None}
        )
    # None of the three wrote a row.
    assert await pool.fetchval(
        "SELECT count(*) FROM life.expiring_items WHERE title LIKE $1", f"{PREFIX}%"
    ) == 0


async def test_update_rejects_blanking_required_fields(pool, today):
    item = await svc.create_expiring_item(
        pool, {"kind": "visa", "title": f"{PREFIX}Schengen", "expires_on": today}
    )
    with pytest.raises(ValueError, match="title cannot be blank"):
        await svc.update_expiring_item(pool, item["id"], {"title": "  "})
    with pytest.raises(ValueError, match="kind cannot be blank"):
        await svc.update_expiring_item(pool, item["id"], {"kind": ""})
    # The row is untouched.
    assert (await svc.get_expiring_item(pool, item["id"]))["title"] == f"{PREFIX}Schengen"


async def test_update_of_a_missing_row_returns_none(pool):
    assert await svc.update_expiring_item(pool, uuid4(), {"notes": "ghost"}) is None
    # An empty patch on a missing row is also None, not a crash.
    assert await svc.update_expiring_item(pool, uuid4(), {}) is None


async def test_list_filters_by_kind_and_orders_by_expiry(pool, today):
    late = await svc.create_expiring_item(
        pool,
        {"kind": "insurance", "title": f"{PREFIX}b-late", "expires_on": today + timedelta(days=90)},
    )
    early = await svc.create_expiring_item(
        pool,
        {"kind": "insurance", "title": f"{PREFIX}a-early", "expires_on": today + timedelta(days=10)},
    )
    other = await svc.create_expiring_item(
        pool,
        {"kind": "domain", "title": f"{PREFIX}c-other", "expires_on": today + timedelta(days=20)},
    )

    mine = [i["id"] for i in await svc.list_expiring_items(pool, "insurance")]
    assert mine == [early["id"], late["id"]], "filtered by kind, soonest expiry first"
    assert other["id"] not in mine

    # Without a filter all three come back — proves the filter above actually
    # filtered rather than the other row simply not existing.
    unfiltered = [i["id"] for i in await svc.list_expiring_items(pool)]
    for item in (early, late, other):
        assert item["id"] in unfiltered
    # kind lookup is case-insensitive because the service lowercases on write
    # AND on query.
    assert [i["id"] for i in await svc.list_expiring_items(pool, "INSURANCE")] == mine


# --------------------------------------------------------------------------
# due_within — the query C6's radar flow runs
# --------------------------------------------------------------------------


async def test_due_within_includes_the_window_and_excludes_beyond_it(pool, today):
    inside = await svc.create_expiring_item(
        pool, {"kind": "visa", "title": f"{PREFIX}in-29", "expires_on": today + timedelta(days=29)}
    )
    on_edge = await svc.create_expiring_item(
        pool, {"kind": "visa", "title": f"{PREFIX}on-30", "expires_on": today + timedelta(days=30)}
    )
    outside = await svc.create_expiring_item(
        pool, {"kind": "visa", "title": f"{PREFIX}out-31", "expires_on": today + timedelta(days=31)}
    )
    far = await svc.create_expiring_item(
        pool, {"kind": "visa", "title": f"{PREFIX}far", "expires_on": today + timedelta(days=400)}
    )

    ids = [i["id"] for i in await svc.due_within(pool, 30)]
    assert inside["id"] in ids
    assert on_edge["id"] in ids, "the boundary day is inclusive"
    assert outside["id"] not in ids
    assert far["id"] not in ids

    # A wider window pulls in the ones excluded above — proves the exclusion
    # was the window doing its job, not the rows failing to exist.
    wide = [i["id"] for i in await svc.due_within(pool, 500)]
    for item in (inside, on_edge, outside, far):
        assert item["id"] in wide


async def test_due_within_surfaces_already_expired_items_with_negative_days_left(pool, today):
    """A lapsed passport is the most urgent row in the table, not the least."""
    expired = await svc.create_expiring_item(
        pool,
        {"kind": "passport", "title": f"{PREFIX}lapsed", "expires_on": today - timedelta(days=45)},
    )
    soon = await svc.create_expiring_item(
        pool, {"kind": "passport", "title": f"{PREFIX}soon", "expires_on": today + timedelta(days=5)}
    )

    rows = {i["id"]: i for i in await svc.due_within(pool, 7)}
    assert expired["id"] in rows, "an expired item dropped out of the radar"
    assert rows[expired["id"]]["days_left"] == -45
    assert rows[soon["id"]]["days_left"] == 5
    # Most overdue first.
    ordered = [i["id"] for i in await svc.due_within(pool, 7) if i["id"] in rows]
    assert ordered == [expired["id"], soon["id"]]


async def test_due_within_zero_is_today_and_the_past_only(pool, today):
    todays = await svc.create_expiring_item(
        pool, {"kind": "medication", "title": f"{PREFIX}today", "expires_on": today}
    )
    tomorrows = await svc.create_expiring_item(
        pool,
        {"kind": "medication", "title": f"{PREFIX}tomorrow", "expires_on": today + timedelta(days=1)},
    )
    ids = [i["id"] for i in await svc.due_within(pool, 0)]
    assert todays["id"] in ids
    assert tomorrows["id"] not in ids
    assert rows_have_lead_days(await svc.due_within(pool, 0), todays["id"])


def rows_have_lead_days(rows: list[dict], item_id) -> bool:
    """due_within must return the full item, not just an id/date pair — C6
    needs `lead_days` to decide which threshold was crossed."""
    row = next(r for r in rows if r["id"] == item_id)
    return row["lead_days"] == [30, 7, 1] and "kind" in row and "title" in row
