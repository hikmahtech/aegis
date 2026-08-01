"""Schema assertions for migration 017 (life.observations).

The session's test database is created empty and migrated from this
checkout's migrations/ (root conftest), so reaching these assertions already
proves the migration applies cleanly to a fresh database. This file pins the
shape it produced, and re-executes the file to prove it is safe on re-run.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import pytest
from aegis.db import run_migrations

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "017_life_observations.sql"


@pytest.mark.asyncio
async def test_life_observations_columns(db_pool: asyncpg.Pool) -> None:
    await run_migrations(db_pool)
    rows = await db_pool.fetch(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'life' AND table_name = 'observations'"
    )
    cols = {r["column_name"]: (r["data_type"], r["is_nullable"]) for r in rows}
    assert cols, "life.observations does not exist"
    assert cols["id"][0] == "uuid"
    assert cols["source"] == ("text", "NO")
    assert cols["metric"] == ("text", "NO")
    # Nullable on purpose: a categorical observation carries metadata only.
    assert cols["value"] == ("numeric", "YES")
    assert cols["observed_at"] == ("timestamp with time zone", "NO")
    assert cols["metadata"] == ("jsonb", "NO")
    assert cols["created_at"] == ("timestamp with time zone", "NO")


@pytest.mark.asyncio
async def test_life_observations_query_indexes(db_pool: asyncpg.Pool) -> None:
    """Both access paths in services/observations.py are indexed: trend/summary
    by (metric, observed_at) and per-source sweeps by (source, observed_at)."""
    await run_migrations(db_pool)
    defs = {
        r["indexname"]: r["indexdef"]
        for r in await db_pool.fetch(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = 'life' AND tablename = 'observations'"
        )
    }
    assert "(metric, observed_at)" in defs.get("idx_life_observations_metric_time", "")
    assert "(source, observed_at)" in defs.get("idx_life_observations_source_time", "")


@pytest.mark.asyncio
async def test_migration_017_is_safe_to_re_run(db_pool: asyncpg.Pool) -> None:
    await run_migrations(db_pool)
    await db_pool.execute(
        "INSERT INTO life.observations (source, metric, value, observed_at) "
        "VALUES ($1, $1, 1, $2)",
        "zzmig017-survives-a-rerun",
        datetime.now(UTC),
    )
    try:
        # Must not raise (CREATE SCHEMA / TABLE / INDEX ... IF NOT EXISTS) and
        # must not drop what is already there.
        await db_pool.execute(MIGRATION.read_text())
        assert await db_pool.fetchval(
            "SELECT 1 FROM life.observations WHERE source = $1", "zzmig017-survives-a-rerun"
        )
    finally:
        await db_pool.execute(
            "DELETE FROM life.observations WHERE source = $1", "zzmig017-survives-a-rerun"
        )
