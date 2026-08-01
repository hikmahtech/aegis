"""Schema assertions for migration 016 (life schema + life.people).

The session's test database is created empty and migrated from this
checkout's migrations/ (root conftest), so reaching these assertions already
proves the migration applies cleanly to a fresh database. This file pins the
shape it produced, and re-executes the file to prove it is safe on re-run —
`schema_migrations` normally skips an applied file, but a hand-run or a
restored-from-backup database must not explode.
"""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest
from aegis.db import run_migrations

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "016_life_people.sql"


@pytest.mark.asyncio
async def test_life_people_columns(db_pool: asyncpg.Pool) -> None:
    await run_migrations(db_pool)
    rows = await db_pool.fetch(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'life' AND table_name = 'people'"
    )
    cols = {r["column_name"]: (r["data_type"], r["is_nullable"]) for r in rows}
    assert cols, "life.people does not exist"
    assert cols["id"][0] == "uuid"
    assert cols["name"] == ("text", "NO")
    assert cols["aliases"] == ("ARRAY", "NO")
    assert cols["relationship"][0] == "text"
    assert cols["key_dates"] == ("jsonb", "NO")
    assert cols["notes"][0] == "text"
    assert cols["last_contact"] == ("timestamp with time zone", "YES")
    assert cols["metadata"] == ("jsonb", "NO")
    assert cols["created_at"] == ("timestamp with time zone", "NO")
    assert cols["updated_at"] == ("timestamp with time zone", "NO")


@pytest.mark.asyncio
async def test_life_people_lookup_indexes(db_pool: asyncpg.Pool) -> None:
    """Both lookup paths in services/people.find_people are indexed."""
    await run_migrations(db_pool)
    defs = {
        r["indexname"]: r["indexdef"]
        for r in await db_pool.fetch(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = 'life' AND tablename = 'people'"
        )
    }
    assert "lower(name)" in defs.get("idx_life_people_name_lower", "")
    assert "gin" in defs.get("idx_life_people_aliases", "").lower()


@pytest.mark.asyncio
async def test_migration_016_is_safe_to_re_run(db_pool: asyncpg.Pool) -> None:
    await run_migrations(db_pool)
    await db_pool.execute(
        "INSERT INTO life.people (name) VALUES ($1)", "zzmig016-survives-a-rerun"
    )
    try:
        # Must not raise (CREATE SCHEMA / TABLE / INDEX ... IF NOT EXISTS) and
        # must not drop what is already there.
        await db_pool.execute(MIGRATION.read_text())
        assert await db_pool.fetchval(
            "SELECT 1 FROM life.people WHERE name = $1", "zzmig016-survives-a-rerun"
        )
    finally:
        await db_pool.execute(
            "DELETE FROM life.people WHERE name = $1", "zzmig016-survives-a-rerun"
        )
