"""Schema assertions for migration 013 (Phase 4 interaction metadata)."""

from __future__ import annotations

import asyncpg
import pytest
from aegis.db import run_migrations


async def _column_exists(conn: asyncpg.Connection, table: str, col: str) -> bool:
    return bool(
        await conn.fetchval(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=$1 AND column_name=$2",
            table,
            col,
        )
    )


async def _index_exists(conn: asyncpg.Connection, name: str) -> bool:
    return bool(
        await conn.fetchval(
            "SELECT 1 FROM pg_indexes WHERE schemaname='public' AND indexname=$1",
            name,
        )
    )


@pytest.mark.asyncio
async def test_interactions_metadata_column_exists(db_pool: asyncpg.Pool) -> None:
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        assert await _column_exists(conn, "interactions", "metadata")
        # default '{}' lands as empty dict
        col_default = await conn.fetchval(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='interactions' "
            "AND column_name='metadata'"
        )
        assert col_default and "'{}'" in col_default


@pytest.mark.asyncio
async def test_interactions_metadata_source_index_exists(db_pool: asyncpg.Pool) -> None:
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        assert await _index_exists(conn, "interactions_metadata_source_idx")


@pytest.mark.asyncio
async def test_interactions_metadata_default_is_empty_dict(db_pool: asyncpg.Pool) -> None:
    """A newly-inserted interaction without metadata should land with {}."""
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM interactions WHERE flow_run_id='M013_TEST'")
        await conn.execute(
            "INSERT INTO interactions "
            "(flow_run_id, agent_id, kind, origin, status, prompt, options) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            "M013_TEST", "sebas", "choice", "test", "pending", "x", {},
        )
        meta = await conn.fetchval(
            "SELECT metadata FROM interactions WHERE flow_run_id='M013_TEST'"
        )
        assert meta == {}
        await conn.execute("DELETE FROM interactions WHERE flow_run_id='M013_TEST'")
