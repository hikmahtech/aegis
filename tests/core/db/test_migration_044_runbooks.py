"""Schema assertions for migration 044 (the `runbooks` table, #499).

The session's test database is migrated from this checkout's migrations/, so
reaching these assertions already proves 044 applies to a fresh database. This
pins the columns the service and the worker read, and re-executes the file to
prove it is safe on re-run (the runner keys on the filename, so a rename runs
it again).
"""

from __future__ import annotations

from pathlib import Path

from aegis.db import run_migrations

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "044_runbooks.sql"


async def test_runbooks_table_shape(db_pool):
    await run_migrations(db_pool)
    cols = {
        r["column_name"]: (r["data_type"], r["is_nullable"])
        for r in await db_pool.fetch(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'runbooks'"
        )
    }
    assert cols == {
        "name_key": ("text", "NO"),
        "name": ("text", "NO"),
        "body": ("text", "NO"),
        "updated_at": ("timestamp with time zone", "NO"),
        "updated_by": ("text", "NO"),
    }
    pk = await db_pool.fetchval(
        "SELECT a.attname FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
        "WHERE i.indrelid = 'runbooks'::regclass AND i.indisprimary"
    )
    assert pk == "name_key"


async def test_migration_044_is_idempotent(db_pool):
    await run_migrations(db_pool)
    sql = MIGRATION.read_text()
    await db_pool.execute(sql)
    await db_pool.execute(sql)
