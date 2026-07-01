"""Schema assertions for migration 021 (slack_channel_id + delivery_ref)."""

from __future__ import annotations

import asyncpg
import pytest
from aegis.db import run_migrations


async def _column_data_type(
    conn: asyncpg.Connection, table: str, col: str
) -> str | None:
    return await conn.fetchval(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=$1 AND column_name=$2",
        table,
        col,
    )


@pytest.mark.asyncio
async def test_migration_021_columns(db_pool: asyncpg.Pool) -> None:
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        slack_type = await _column_data_type(conn, "agents", "slack_channel_id")
        assert slack_type == "text", (
            f"agents.slack_channel_id expected data_type 'text', got {slack_type!r}"
        )

        delivery_type = await _column_data_type(conn, "interactions", "delivery_ref")
        assert delivery_type == "jsonb", (
            f"interactions.delivery_ref expected data_type 'jsonb', got {delivery_type!r}"
        )
