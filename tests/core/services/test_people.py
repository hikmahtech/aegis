"""services/people.py — CRUD + lookup over life.people (migration 016).

Real Postgres (the session's freshly-migrated test database via `db_pool`);
no mocks, so the SQL, the `life` schema and both indexes are exercised.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from aegis.db import run_migrations
from aegis.services import people as svc

# Prefix every fixture row so the assertions can't be satisfied (or broken) by
# rows another test in the shared database left behind.
PREFIX = "zzpeople-"


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    await run_migrations(db_pool)
    await db_pool.execute("DELETE FROM life.people WHERE name LIKE $1", f"{PREFIX}%")
    yield db_pool
    await db_pool.execute("DELETE FROM life.people WHERE name LIKE $1", f"{PREFIX}%")


async def test_create_get_update_delete_round_trip(pool):
    created = await svc.create_person(
        pool,
        {
            "name": f"{PREFIX}Amina Yusuf",
            "aliases": ["Ammu", "amina@example.com"],
            "relationship": "sister",
            "key_dates": {"birthday": "1989-03-14"},
            "notes": "allergic to walnuts",
            "last_contact": datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
            "metadata": {"source": "manual"},
        },
    )
    assert created["name"] == f"{PREFIX}Amina Yusuf"
    assert created["relationship"] == "sister"
    # jsonb columns come back as Python objects, not strings (pool codec).
    assert created["key_dates"] == {"birthday": "1989-03-14"}
    assert created["metadata"] == {"source": "manual"}
    assert created["notes"] == "allergic to walnuts"
    assert created["last_contact"] == datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    assert created["created_at"] is not None

    fetched = await svc.get_person(pool, created["id"])
    assert fetched is not None
    assert fetched["id"] == created["id"]
    assert fetched["name"] == created["name"]

    listed = await svc.list_people(pool)
    assert created["id"] in [p["id"] for p in listed]

    updated = await svc.update_person(
        pool,
        created["id"],
        {"relationship": "elder sister", "notes": "walnut allergy; loves biryani"},
    )
    assert updated is not None
    assert updated["relationship"] == "elder sister"
    assert updated["notes"] == "walnut allergy; loves biryani"
    # Untouched fields survive a partial update.
    assert updated["key_dates"] == {"birthday": "1989-03-14"}
    assert updated["aliases"] == ["ammu", "amina@example.com"]

    assert await svc.delete_person(pool, created["id"]) is True
    assert await svc.get_person(pool, created["id"]) is None


async def test_aliases_are_lowercased_on_write(pool):
    """Aliases are stored lowercased so `find_people` can use the GIN
    containment index instead of scanning + lower()-ing every array."""
    created = await svc.create_person(
        pool, {"name": f"{PREFIX}Case Test", "aliases": ["  MixedCase  ", "UPPER@Example.COM"]}
    )
    assert created["aliases"] == ["mixedcase", "upper@example.com"]

    updated = await svc.update_person(pool, created["id"], {"aliases": ["Second", "SECOND", ""]})
    assert updated is not None
    # Blanks dropped, duplicates collapsed.
    assert updated["aliases"] == ["second"]


async def test_find_people_matches_alias_not_just_name(pool):
    target = await svc.create_person(
        pool,
        {"name": f"{PREFIX}Rafiq Khan", "aliases": ["raf", "rafiq.khan@example.com"]},
    )
    # A decoy sharing neither name nor alias — proves a match is a real match
    # and not "the only row in the table".
    decoy = await svc.create_person(pool, {"name": f"{PREFIX}Someone Else", "aliases": ["else"]})

    by_name = await svc.find_people(pool, f"{PREFIX}Rafiq Khan")
    assert [p["id"] for p in by_name] == [target["id"]]

    # Name match is case-insensitive.
    by_name_case = await svc.find_people(pool, f"{PREFIX}rafiq khan".upper())
    assert [p["id"] for p in by_name_case] == [target["id"]]

    # The point of this test: an alias — which is NOT the name — finds them.
    by_alias = await svc.find_people(pool, "rafiq.khan@example.com")
    assert [p["id"] for p in by_alias] == [target["id"]]

    # ...case-insensitively, in the caller's casing.
    by_alias_case = await svc.find_people(pool, "Rafiq.Khan@Example.COM")
    assert [p["id"] for p in by_alias_case] == [target["id"]]

    # The decoy is reachable by its own alias, so the query isn't just
    # returning the first row it finds.
    assert [p["id"] for p in await svc.find_people(pool, "else")] == [decoy["id"]]

    # No match => empty, while the table is demonstrably not empty (otherwise
    # "returns []" would prove nothing).
    assert await svc.find_people(pool, "nobody@nowhere.example") == []
    assert len(await svc.list_people(pool)) >= 2


async def test_find_people_blank_query_never_reaches_the_database():
    """A blank/None search short-circuits before the query runs.

    Asserting only `== []` would be vacuous — the SQL returns no rows for an
    empty needle anyway, so that assertion passes with the guard deleted
    (proved by break-and-revert). Hand it a pool that explodes on use, so the
    test fails if the guard ever goes away. `None` would also crash on
    `.strip()` without it.
    """

    class ExplodingPool:
        async def fetch(self, *args, **kwargs):
            raise AssertionError("find_people queried the database for a blank search")

    for blank in ("", "   ", None):
        assert await svc.find_people(ExplodingPool(), blank) == []


async def test_delete_missing_id_is_a_no_op_not_an_error(pool):
    # Must not raise — a delete of an already-deleted person is benign.
    assert await svc.delete_person(pool, uuid4()) is False


async def test_update_missing_id_returns_none(pool):
    assert await svc.update_person(pool, uuid4(), {"name": f"{PREFIX}Ghost"}) is None


async def test_create_requires_a_name(pool):
    with pytest.raises(ValueError, match="name is required"):
        await svc.create_person(pool, {"name": "   ", "relationship": "friend"})
    with pytest.raises(ValueError, match="name is required"):
        await svc.create_person(pool, {})


async def test_update_rejects_blanking_the_name(pool):
    created = await svc.create_person(pool, {"name": f"{PREFIX}Keeps Name"})
    with pytest.raises(ValueError, match="name cannot be blank"):
        await svc.update_person(pool, created["id"], {"name": "  "})
    still = await svc.get_person(pool, created["id"])
    assert still is not None and still["name"] == f"{PREFIX}Keeps Name"


# The companion guard — that life.people is deliberately absent from the
# cleanup retention maps — lives in tests/worker/test_cleanup_activity.py,
# because CI's core job installs only `core[dev]` and cannot import
# aegis_worker.
