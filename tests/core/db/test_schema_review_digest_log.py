"""Schema assertions for review_digest_log (Phase 5), seeded in
migrations/001_baseline.sql — not a standalone numbered migration."""

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
async def test_review_digest_log_table(db_pool: asyncpg.Pool) -> None:
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        for col in (
            "id", "review_kind", "counts", "preview", "interaction_id",
            "user_choice", "acknowledged", "created_at", "acknowledged_at",
        ):
            assert await _column_exists(conn, "review_digest_log", col), f"missing {col}"


@pytest.mark.asyncio
async def test_review_digest_log_indexes(db_pool: asyncpg.Pool) -> None:
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        assert await _index_exists(conn, "review_digest_log_kind_created_idx")
        assert await _index_exists(conn, "review_digest_log_interaction_idx")


@pytest.mark.asyncio
async def test_review_digest_log_defaults(db_pool: asyncpg.Pool) -> None:
    """Default counts={} and acknowledged=false should land via INSERT."""
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM review_digest_log WHERE review_kind='M014_TEST'"
        )
        row_id = await conn.fetchval(
            "INSERT INTO review_digest_log (review_kind) VALUES ($1) RETURNING id",
            "M014_TEST",
        )
        row = await conn.fetchrow(
            "SELECT counts, acknowledged FROM review_digest_log WHERE id=$1", row_id
        )
        assert row["counts"] == {}
        assert row["acknowledged"] is False
        await conn.execute("DELETE FROM review_digest_log WHERE review_kind='M014_TEST'")
