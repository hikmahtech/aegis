"""Schema assertions for migration 018 (life.expiring_items + its alert ledger).

The session's test database is created empty and migrated from this
checkout's migrations/ (root conftest), so reaching these assertions already
proves the migration applies cleanly to a fresh database. This file pins the
shape it produced, proves the dedup unique index and the FK behaviours the
service relies on, and re-executes the file to prove it is safe on re-run.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from aegis.db import run_migrations

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "018_life_expiring_items.sql"

PREFIX = "zzmig018-"


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    await run_migrations(db_pool)
    # Children before parents: expiring_item_alerts -> expiring_items -> people.
    await _wipe(db_pool)
    yield db_pool
    await _wipe(db_pool)


async def _wipe(pool: asyncpg.Pool) -> None:
    await pool.execute(
        "DELETE FROM life.expiring_item_alerts WHERE item_id IN "
        "(SELECT id FROM life.expiring_items WHERE title LIKE $1)",
        f"{PREFIX}%",
    )
    await pool.execute("DELETE FROM life.expiring_items WHERE title LIKE $1", f"{PREFIX}%")
    await pool.execute("DELETE FROM life.people WHERE name LIKE $1", f"{PREFIX}%")


@pytest.mark.asyncio
async def test_life_expiring_items_columns(pool: asyncpg.Pool) -> None:
    rows = await pool.fetch(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'life' AND table_name = 'expiring_items'"
    )
    cols = {r["column_name"]: (r["data_type"], r["is_nullable"]) for r in rows}
    assert cols, "life.expiring_items does not exist"
    assert cols["id"][0] == "uuid"
    assert cols["kind"] == ("text", "NO")
    assert cols["title"] == ("text", "NO")
    assert cols["expires_on"] == ("date", "NO")
    assert cols["lead_days"] == ("ARRAY", "NO")
    assert cols["person_id"] == ("uuid", "YES")
    assert cols["asset_id"] == ("uuid", "YES")
    assert cols["notes"][0] == "text"
    assert cols["metadata"] == ("jsonb", "NO")
    assert cols["created_at"] == ("timestamp with time zone", "NO")
    assert cols["updated_at"] == ("timestamp with time zone", "NO")


@pytest.mark.asyncio
async def test_lead_days_column_default(pool: asyncpg.Pool) -> None:
    """A row inserted without lead_days still gets the {30,7,1} warning ladder."""
    item_id = await pool.fetchval(
        "INSERT INTO life.expiring_items (kind, title, expires_on) "
        "VALUES ('passport', $1, '2030-01-01') RETURNING id",
        f"{PREFIX}default-leads",
    )
    assert await pool.fetchval(
        "SELECT lead_days FROM life.expiring_items WHERE id = $1", item_id
    ) == [30, 7, 1]


@pytest.mark.asyncio
async def test_alert_dedup_unique_index_rejects_a_duplicate(pool: asyncpg.Pool) -> None:
    """(item_id, threshold_days, expires_on) is unique ACROSS ALL TIME.

    This is the shape migration 013 arrived at after #113 (a day-bucketed
    dedup refired 166 times in 3 weeks). Same item + same threshold + same
    expiry cycle must be insertable exactly once, no matter how much later
    the second attempt happens.
    """
    item_id = await pool.fetchval(
        "INSERT INTO life.expiring_items (kind, title, expires_on) "
        "VALUES ('visa', $1, '2030-06-01') RETURNING id",
        f"{PREFIX}dedup",
    )
    await pool.execute(
        "INSERT INTO life.expiring_item_alerts (item_id, threshold_days, expires_on) "
        "VALUES ($1, 30, '2030-06-01')",
        item_id,
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        # A different fired_at must NOT make it a new row.
        await pool.execute(
            "INSERT INTO life.expiring_item_alerts "
            "(item_id, threshold_days, expires_on, fired_at) "
            "VALUES ($1, 30, '2030-06-01', now() + interval '400 days')",
            item_id,
        )

    # ...but a different threshold, and a renewed expiry cycle, both fire.
    await pool.execute(
        "INSERT INTO life.expiring_item_alerts (item_id, threshold_days, expires_on) "
        "VALUES ($1, 7, '2030-06-01')",
        item_id,
    )
    await pool.execute(
        "INSERT INTO life.expiring_item_alerts (item_id, threshold_days, expires_on) "
        "VALUES ($1, 30, '2040-06-01')",
        item_id,
    )
    assert (
        await pool.fetchval(
            "SELECT count(*) FROM life.expiring_item_alerts WHERE item_id = $1", item_id
        )
        == 3
    )


@pytest.mark.asyncio
async def test_deleting_an_item_cascades_its_alert_ledger(pool: asyncpg.Pool) -> None:
    item_id = await pool.fetchval(
        "INSERT INTO life.expiring_items (kind, title, expires_on) "
        "VALUES ('warranty', $1, '2030-02-02') RETURNING id",
        f"{PREFIX}cascade",
    )
    await pool.execute(
        "INSERT INTO life.expiring_item_alerts (item_id, threshold_days, expires_on) "
        "VALUES ($1, 30, '2030-02-02')",
        item_id,
    )
    await pool.execute("DELETE FROM life.expiring_items WHERE id = $1", item_id)
    assert (
        await pool.fetchval(
            "SELECT count(*) FROM life.expiring_item_alerts WHERE item_id = $1", item_id
        )
        == 0
    )


@pytest.mark.asyncio
async def test_deleting_a_person_nulls_the_link_instead_of_blocking(pool: asyncpg.Pool) -> None:
    """ON DELETE SET NULL, not RESTRICT.

    services/people.delete_person issues a bare `DELETE FROM life.people`; a
    RESTRICT foreign key here would turn "delete a person who owns a tracked
    document" into an unhandled ForeignKeyViolation (HTTP 500) on the
    unrelated People page. The document itself must survive with the owner
    detached.
    """
    person_id = await pool.fetchval(
        "INSERT INTO life.people (name) VALUES ($1) RETURNING id", f"{PREFIX}Owner"
    )
    item_id = await pool.fetchval(
        "INSERT INTO life.expiring_items (kind, title, expires_on, person_id) "
        "VALUES ('licence', $1, '2030-03-03', $2) RETURNING id",
        f"{PREFIX}orphan-me",
        person_id,
    )
    await pool.execute("DELETE FROM life.people WHERE id = $1", person_id)
    row = await pool.fetchrow(
        "SELECT title, person_id FROM life.expiring_items WHERE id = $1", item_id
    )
    assert row is not None, "the item was deleted along with its person"
    assert row["person_id"] is None


@pytest.mark.asyncio
async def test_due_window_index_exists(pool: asyncpg.Pool) -> None:
    """The radar's only hot query (`expires_on <= ...`) is indexed."""
    defs = {
        r["indexname"]: r["indexdef"]
        for r in await pool.fetch(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = 'life' AND tablename = 'expiring_items'"
        )
    }
    # Assert the INDEXED EXPRESSION, not a substring of the whole indexdef:
    # indexdef embeds the index NAME, so `"expires_on" in indexdef` is true
    # even for an index built on a completely different column.
    assert "btree (expires_on)" in defs.get("idx_life_expiring_items_expires_on", "")
    assert "btree (person_id)" in defs.get("idx_life_expiring_items_person", "")


@pytest.mark.asyncio
async def test_migration_018_is_safe_to_re_run(pool: asyncpg.Pool) -> None:
    item_id = await pool.fetchval(
        "INSERT INTO life.expiring_items (kind, title, expires_on) "
        "VALUES ('domain', $1, $2) RETURNING id",
        f"{PREFIX}survives-a-rerun",
        date(2030, 12, 31),
    )
    await pool.execute(
        "INSERT INTO life.expiring_item_alerts (item_id, threshold_days, expires_on) "
        "VALUES ($1, 30, '2030-12-31')",
        item_id,
    )
    # Must not raise (CREATE SCHEMA / TABLE / INDEX ... IF NOT EXISTS) and must
    # not drop what is already there.
    await pool.execute(MIGRATION.read_text())
    assert await pool.fetchval(
        "SELECT 1 FROM life.expiring_items WHERE id = $1", item_id
    )
    assert await pool.fetchval(
        "SELECT 1 FROM life.expiring_item_alerts WHERE item_id = $1", item_id
    )
