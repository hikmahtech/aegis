"""services/assets.py — CRUD over life.assets (019) + the service-due mirror.

Real Postgres (the session's freshly-migrated test database via `db_pool`);
no mocks, so the SQL, the `life` schema, the slug UNIQUE and the asset_id
foreign key are all exercised for real.
"""

from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from aegis.db import run_migrations
from aegis.services import assets as svc

# Prefix every fixture row so the assertions can't be satisfied (or broken) by
# rows another test in the shared database left behind.
PREFIX = "zzc7svc-"


async def _wipe(pool: asyncpg.Pool) -> None:
    # Children before parents: alerts -> expiring_items -> assets. Matching
    # expiring_items on BOTH the prefix and "belongs to one of my assets",
    # because the mirror titles rows "Service due: <name>".
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
async def pool(db_pool):
    await run_migrations(db_pool)
    await _wipe(db_pool)
    yield db_pool
    await _wipe(db_pool)


async def _mirror(pool: asyncpg.Pool, asset_id) -> dict | None:
    row = await pool.fetchrow(
        "SELECT id, kind, title, expires_on, lead_days FROM life.expiring_items "
        "WHERE asset_id = $1 AND kind = 'asset_service'",
        asset_id,
    )
    return dict(row) if row else None


# --------------------------------------------------------------------------
# slugify
# --------------------------------------------------------------------------


def test_slugify_normalises_and_never_returns_empty():
    assert svc.slugify("  Bosch Washing Machine  ") == "bosch-washing-machine"
    assert svc.slugify("Volvo XC90 (2019)") == "volvo-xc90-2019"
    # A name made entirely of punctuation must still produce a usable slug —
    # the column is NOT NULL and UNIQUE, so "" would be an insert error.
    assert svc.slugify("!!!") == "asset"


# --------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------


async def test_create_get_update_delete_round_trip(pool):
    created = await svc.create_asset(
        pool,
        {
            "name": f"{PREFIX}Bosch Washer",
            "kind": "  Appliance  ",
            "purchase_date": date(2022, 3, 1),
            "warranty_until": date(2027, 3, 1),
            "location": "utility room",
            "notes": "model WAT24",
            "metadata": {"serial": "abc"},
        },
    )
    # kind is lowercased and stripped; slug is derived from the name.
    assert created["kind"] == "appliance"
    assert created["slug"] == f"{PREFIX}bosch-washer"
    assert created["name"] == f"{PREFIX}Bosch Washer"
    assert created["purchase_date"] == date(2022, 3, 1)
    assert created["warranty_until"] == date(2027, 3, 1)
    assert created["location"] == "utility room"
    # jsonb comes back as a Python object, not a string (pool codec).
    assert created["metadata"] == {"serial": "abc"}
    assert created["created_at"] is not None
    # No service schedule -> no mirror row.
    assert await _mirror(pool, created["id"]) is None

    fetched = await svc.get_asset(pool, created["id"])
    assert fetched is not None
    assert fetched["id"] == created["id"]
    assert fetched["slug"] == created["slug"]

    listed = await svc.list_assets(pool)
    assert created["id"] in [a["id"] for a in listed]

    updated = await svc.update_asset(
        pool, created["id"], {"location": "garage", "notes": "moved"}
    )
    assert updated is not None
    assert updated["location"] == "garage"
    assert updated["notes"] == "moved"
    # Untouched fields survive a partial patch.
    assert updated["kind"] == "appliance"
    assert updated["slug"] == created["slug"]

    assert await svc.delete_asset(pool, created["id"]) is True
    assert await svc.get_asset(pool, created["id"]) is None
    # Deleting an already-gone row is False, not an exception.
    assert await svc.delete_asset(pool, created["id"]) is False


async def test_slug_collisions_are_suffixed_not_raised(pool):
    """Two assets with the same name must not blow up on the UNIQUE index —
    the create route does not catch UniqueViolationError, so that would be a
    500 instead of a second fridge."""
    first = await svc.create_asset(pool, {"name": f"{PREFIX}Fridge", "kind": "appliance"})
    second = await svc.create_asset(pool, {"name": f"{PREFIX}Fridge", "kind": "appliance"})
    third = await svc.create_asset(pool, {"name": f"{PREFIX}fridge!", "kind": "appliance"})
    assert first["slug"] == f"{PREFIX}fridge"
    assert second["slug"] == f"{PREFIX}fridge-2"
    assert third["slug"] == f"{PREFIX}fridge-3"


async def test_explicit_slug_is_honoured_and_normalised(pool):
    asset = await svc.create_asset(
        pool, {"name": f"{PREFIX}Boiler", "kind": "hvac", "slug": f"{PREFIX}Main Boiler"}
    )
    assert asset["slug"] == f"{PREFIX}main-boiler"


async def test_create_requires_name_and_kind(pool):
    with pytest.raises(ValueError, match="name is required"):
        await svc.create_asset(pool, {"name": "   ", "kind": "car"})
    with pytest.raises(ValueError, match="kind is required"):
        await svc.create_asset(pool, {"name": f"{PREFIX}x", "kind": "  "})
    assert (
        await pool.fetchval("SELECT count(*) FROM life.assets WHERE slug LIKE $1", f"{PREFIX}%")
        == 0
    )


async def test_update_rejects_blanking_required_fields(pool):
    asset = await svc.create_asset(pool, {"name": f"{PREFIX}Car", "kind": "car"})
    with pytest.raises(ValueError, match="name cannot be blank"):
        await svc.update_asset(pool, asset["id"], {"name": "  "})
    with pytest.raises(ValueError, match="kind cannot be blank"):
        await svc.update_asset(pool, asset["id"], {"kind": ""})
    assert (await svc.get_asset(pool, asset["id"]))["name"] == f"{PREFIX}Car"


async def test_update_of_a_missing_row_returns_none(pool):
    assert await svc.update_asset(pool, uuid4(), {"location": "ghost"}) is None
    # An empty patch on a missing row is also None, not a crash.
    assert await svc.update_asset(pool, uuid4(), {}) is None


async def test_list_filters_by_kind(pool):
    car = await svc.create_asset(pool, {"name": f"{PREFIX}a-Volvo", "kind": "car"})
    other_car = await svc.create_asset(pool, {"name": f"{PREFIX}b-Honda", "kind": "car"})
    fridge = await svc.create_asset(pool, {"name": f"{PREFIX}c-Fridge", "kind": "appliance"})

    cars = [a["id"] for a in await svc.list_assets(pool, "car")]
    assert cars == [car["id"], other_car["id"]], "filtered by kind, alphabetical"
    assert fridge["id"] not in cars

    # Unfiltered returns all three — proves the filter above actually filtered
    # rather than the other row simply not existing.
    everything = [a["id"] for a in await svc.list_assets(pool)]
    for asset in (car, other_car, fridge):
        assert asset["id"] in everything
    # Case-insensitive because the service lowercases on write AND on query.
    assert [a["id"] for a in await svc.list_assets(pool, "CAR")] == cars


# --------------------------------------------------------------------------
# service_due_on / the life.expiring_items mirror
# --------------------------------------------------------------------------


def test_service_due_on_needs_both_inputs():
    base = {"last_serviced_at": date(2026, 1, 1), "service_interval_days": 90}
    assert svc.service_due_on(base) == date(2026, 4, 1)
    # An interval with no anchor date, and a date with no interval, are both
    # "no schedule" — not a reminder due today.
    assert svc.service_due_on({"service_interval_days": 90}) is None
    assert svc.service_due_on({"last_serviced_at": date(2026, 1, 1)}) is None
    assert svc.service_due_on({**base, "service_interval_days": 0}) is None
    assert svc.service_due_on({**base, "service_interval_days": -30}) is None
    assert svc.service_due_on({**base, "service_interval_days": "junk"}) is None


async def test_create_with_a_service_schedule_mirrors_into_the_expiry_radar(pool):
    serviced = date(2026, 1, 10)
    asset = await svc.create_asset(
        pool,
        {
            "name": f"{PREFIX}Boiler",
            "kind": "hvac",
            "service_interval_days": 365,
            "last_serviced_at": serviced,
        },
    )
    mirror = await _mirror(pool, asset["id"])
    assert mirror is not None, "no life.expiring_items row was created for the asset"
    assert mirror["expires_on"] == serviced + timedelta(days=365)
    assert mirror["title"] == f"Service due: {PREFIX}Boiler"
    # Inherits the column default warning ladder, so C6's radar treats it like
    # any other tracked item.
    assert mirror["lead_days"] == [30, 7, 1]


async def test_marking_it_serviced_again_moves_the_due_date_without_duplicating(pool):
    """Re-servicing must MOVE expires_on, not add a second reminder — and
    moving expires_on is what re-arms every alert threshold (the dedup key in
    life.expiring_item_alerts includes expires_on, migration 018)."""
    asset = await svc.create_asset(
        pool,
        {
            "name": f"{PREFIX}Car",
            "kind": "car",
            "service_interval_days": 180,
            "last_serviced_at": date(2026, 1, 1),
        },
    )
    first = await _mirror(pool, asset["id"])
    assert first is not None, "no service reminder was created"
    assert first["expires_on"] == date(2026, 6, 30)

    await svc.update_asset(pool, asset["id"], {"last_serviced_at": date(2026, 7, 1)})
    second = await _mirror(pool, asset["id"])
    assert second["id"] == first["id"], "a second reminder row was created"
    assert second["expires_on"] == date(2026, 12, 28)
    assert (
        await pool.fetchval(
            "SELECT count(*) FROM life.expiring_items WHERE asset_id = $1", asset["id"]
        )
        == 1
    )


async def test_adding_a_schedule_to_an_existing_asset_creates_the_mirror(pool):
    """The mirror is reconciled on UPDATE too, not only on create."""
    asset = await svc.create_asset(pool, {"name": f"{PREFIX}Dishwasher", "kind": "appliance"})
    assert await _mirror(pool, asset["id"]) is None

    await svc.update_asset(
        pool,
        asset["id"],
        {"service_interval_days": 90, "last_serviced_at": date(2026, 2, 1)},
    )
    mirror = await _mirror(pool, asset["id"])
    assert mirror is not None
    assert mirror["expires_on"] == date(2026, 5, 2)


async def test_clearing_the_schedule_removes_the_mirror(pool):
    """Explicit null on a mirror input means "switch the reminder off" — a
    reminder computed from inputs that no longer exist would nag forever."""
    asset = await svc.create_asset(
        pool,
        {
            "name": f"{PREFIX}Filter",
            "kind": "appliance",
            "service_interval_days": 30,
            "last_serviced_at": date(2026, 1, 1),
        },
    )
    assert await _mirror(pool, asset["id"]) is not None

    updated = await svc.update_asset(pool, asset["id"], {"service_interval_days": None})
    assert updated["service_interval_days"] is None
    assert await _mirror(pool, asset["id"]) is None


async def test_a_partial_patch_does_not_clear_the_schedule(pool):
    """The clear-on-null exception is scoped to the two mirror inputs: a patch
    that simply doesn't mention them must leave the reminder alone."""
    asset = await svc.create_asset(
        pool,
        {
            "name": f"{PREFIX}Aircon",
            "kind": "hvac",
            "service_interval_days": 200,
            "last_serviced_at": date(2026, 1, 1),
        },
    )
    before = await _mirror(pool, asset["id"])
    await svc.update_asset(pool, asset["id"], {"location": "bedroom"})
    after = await _mirror(pool, asset["id"])
    assert after is not None, "an unrelated patch deleted the service reminder"
    assert after["expires_on"] == before["expires_on"]


async def test_renaming_the_asset_retitles_its_reminder(pool):
    asset = await svc.create_asset(
        pool,
        {
            "name": f"{PREFIX}Old Name",
            "kind": "appliance",
            "service_interval_days": 100,
            "last_serviced_at": date(2026, 1, 1),
        },
    )
    await svc.update_asset(pool, asset["id"], {"name": f"{PREFIX}New Name"})
    mirror = await _mirror(pool, asset["id"])
    assert mirror is not None, "the rename deleted the service reminder"
    assert mirror["title"] == f"Service due: {PREFIX}New Name"


async def test_delete_takes_the_mirror_but_leaves_hand_curated_items_intact(pool):
    """The two halves of the delete contract, in one test:

    - the machine-generated `asset_service` row goes (with asset_id NULLed by
      the FK it could never be refreshed, so it would nag forever);
    - a hand-written row that merely references the asset survives, detached.
      That second half is also the proof that the bare DELETE cannot raise
      ForeignKeyViolation and 500 the Assets page.
    """
    asset = await svc.create_asset(
        pool,
        {
            "name": f"{PREFIX}Sold Car",
            "kind": "car",
            "service_interval_days": 180,
            "last_serviced_at": date(2026, 1, 1),
        },
    )
    mirror = await _mirror(pool, asset["id"])
    assert mirror is not None
    curated_id = await pool.fetchval(
        "INSERT INTO life.expiring_items (kind, title, expires_on, asset_id) "
        "VALUES ('insurance', $1, '2030-01-01', $2) RETURNING id",
        f"{PREFIX}car insurance",
        asset["id"],
    )

    assert await svc.delete_asset(pool, asset["id"]) is True

    assert (
        await pool.fetchval("SELECT 1 FROM life.expiring_items WHERE id = $1", mirror["id"])
        is None
    ), "the machine-generated service reminder outlived its asset"
    row = await pool.fetchrow(
        "SELECT title, asset_id FROM life.expiring_items WHERE id = $1", curated_id
    )
    assert row is not None, "a hand-curated document was deleted with its asset"
    assert row["asset_id"] is None
