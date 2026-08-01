"""Schema assertions for migration 019 (life.assets + the asset_id FK it wires up).

The session's test database is created empty and migrated from this
checkout's migrations/ (root conftest), so reaching these assertions already
proves the migration applies cleanly to a fresh database. This file pins the
shape it produced, proves the foreign-key behaviour the service relies on
(the one that keeps a delete from 500-ing), and re-executes the file to prove
it is safe on re-run.
"""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from aegis.db import run_migrations

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "019_life_assets.sql"

PREFIX = "zzc7mig019-"


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    await run_migrations(db_pool)
    await _wipe(db_pool)
    yield db_pool
    await _wipe(db_pool)


async def _wipe(pool: asyncpg.Pool) -> None:
    # Children before parents: alerts -> expiring_items -> assets. The
    # expiring_items sweep matches BOTH the prefix and "belongs to one of my
    # assets", because the service-due mirror titles rows "Service due: <name>"
    # which does not start with the prefix.
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


async def _make_asset(pool: asyncpg.Pool, suffix: str):
    return await pool.fetchval(
        "INSERT INTO life.assets (slug, name, kind) VALUES ($1, $2, 'appliance') RETURNING id",
        f"{PREFIX}{suffix}",
        f"{PREFIX}{suffix}",
    )


@pytest.mark.asyncio
async def test_life_assets_columns(pool: asyncpg.Pool) -> None:
    rows = await pool.fetch(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'life' AND table_name = 'assets'"
    )
    cols = {r["column_name"]: (r["data_type"], r["is_nullable"]) for r in rows}
    assert cols, "life.assets does not exist"
    assert cols["id"][0] == "uuid"
    assert cols["slug"] == ("text", "NO")
    assert cols["name"] == ("text", "NO")
    assert cols["kind"] == ("text", "NO")
    assert cols["purchase_date"] == ("date", "YES")
    assert cols["warranty_until"] == ("date", "YES")
    assert cols["service_interval_days"] == ("integer", "YES")
    assert cols["last_serviced_at"] == ("date", "YES")
    assert cols["location"][0] == "text"
    assert cols["notes"][0] == "text"
    assert cols["metadata"] == ("jsonb", "NO")
    assert cols["created_at"] == ("timestamp with time zone", "NO")
    assert cols["updated_at"] == ("timestamp with time zone", "NO")


@pytest.mark.asyncio
async def test_slug_is_unique(pool: asyncpg.Pool) -> None:
    """The UNIQUE that `services/assets._unique_slug` exists to stay ahead of."""
    await _make_asset(pool, "dup")
    with pytest.raises(asyncpg.UniqueViolationError):
        await pool.execute(
            "INSERT INTO life.assets (slug, name, kind) VALUES ($1, 'other', 'car')",
            f"{PREFIX}dup",
        )


@pytest.mark.asyncio
async def test_kind_and_asset_indexes_exist(pool: asyncpg.Pool) -> None:
    """Assert the INDEXED EXPRESSION, not a substring of the whole indexdef:
    indexdef embeds the index NAME, so `"kind" in indexdef` is true even for an
    index built on a completely different column."""
    asset_defs = {
        r["indexname"]: r["indexdef"]
        for r in await pool.fetch(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = 'life' AND tablename = 'assets'"
        )
    }
    assert "btree (kind)" in asset_defs.get("idx_life_assets_kind", "")

    item_defs = {
        r["indexname"]: r["indexdef"]
        for r in await pool.fetch(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = 'life' AND tablename = 'expiring_items'"
        )
    }
    # Walked by the FK's SET NULL sweep and by the service-due mirror lookup.
    assert "btree (asset_id)" in item_defs.get("idx_life_expiring_items_asset", "")


@pytest.mark.asyncio
async def test_asset_id_foreign_key_is_on_delete_set_null(pool: asyncpg.Pool) -> None:
    """Migration 018 left asset_id a bare uuid "waiting for C7". 019 wires it.

    Pin the confdeltype directly ('n' = SET NULL): a behaviour test alone
    could not distinguish SET NULL from a missing constraint, and RESTRICT
    ('r', the default when you forget) would make services/assets.delete_asset
    — a bare DELETE — raise ForeignKeyViolation, i.e. HTTP 500 on the Assets
    page. CASCADE ('c') would silently delete user-curated documents.
    """
    row = await pool.fetchrow(
        # confdeltype is a "char" column — cast, or asyncpg hands back b'n'.
        "SELECT confdeltype::text AS confdeltype, confrelid::regclass::text AS refs "
        "FROM pg_constraint "
        "WHERE conname = 'life_expiring_items_asset_id_fkey' "
        "AND conrelid = 'life.expiring_items'::regclass"
    )
    assert row is not None, "life.expiring_items.asset_id has no foreign key"
    assert row["refs"] == "life.assets"
    assert row["confdeltype"] == "n", f"expected ON DELETE SET NULL, got {row['confdeltype']!r}"


@pytest.mark.asyncio
async def test_deleting_an_asset_detaches_its_items_instead_of_blocking(
    pool: asyncpg.Pool,
) -> None:
    """A bare DELETE of an asset that owns an expiring item must succeed, and
    the item must survive — the exact hazard person_id was given SET NULL for
    in 018 (a RESTRICT there would 500 the People page)."""
    asset_id = await _make_asset(pool, "owner")
    item_id = await pool.fetchval(
        "INSERT INTO life.expiring_items (kind, title, expires_on, asset_id) "
        "VALUES ('warranty', $1, '2030-03-03', $2) RETURNING id",
        f"{PREFIX}orphan-me",
        asset_id,
    )
    # No try/except: a RESTRICT FK would raise ForeignKeyViolationError here,
    # which is what the Assets page would surface as a 500.
    await pool.execute("DELETE FROM life.assets WHERE id = $1", asset_id)

    row = await pool.fetchrow(
        "SELECT title, asset_id FROM life.expiring_items WHERE id = $1", item_id
    )
    assert row is not None, "the expiring item was deleted along with its asset"
    assert row["asset_id"] is None


@pytest.mark.asyncio
async def test_asset_id_must_reference_a_real_asset(pool: asyncpg.Pool) -> None:
    """The FK is enforced, not just declared — proves the ALTER TABLE ran and
    validated rather than being silently skipped by the re-run guard."""
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await pool.execute(
            "INSERT INTO life.expiring_items (kind, title, expires_on, asset_id) "
            "VALUES ('warranty', $1, '2030-04-04', "
            "'00000000-0000-0000-0000-0000000000c7')",
            f"{PREFIX}dangling",
        )


@pytest.mark.asyncio
async def test_migration_019_is_safe_to_re_run(pool: asyncpg.Pool) -> None:
    asset_id = await _make_asset(pool, "survives-a-rerun")
    item_id = await pool.fetchval(
        "INSERT INTO life.expiring_items (kind, title, expires_on, asset_id) "
        "VALUES ('asset_service', $1, '2030-12-31', $2) RETURNING id",
        f"{PREFIX}mirror",
        asset_id,
    )
    # Must not raise: the ADD CONSTRAINT is guarded (Postgres has no
    # ADD CONSTRAINT IF NOT EXISTS) and everything else is IF NOT EXISTS.
    await pool.execute(MIGRATION.read_text())

    assert await pool.fetchval("SELECT 1 FROM life.assets WHERE id = $1", asset_id)
    # The dangling-pointer cleanup in the migration must NOT null out a link
    # that now resolves.
    assert (
        await pool.fetchval("SELECT asset_id FROM life.expiring_items WHERE id = $1", item_id)
        == asset_id
    )
