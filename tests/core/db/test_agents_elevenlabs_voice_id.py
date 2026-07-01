"""Schema assertion for migration 022 (agents.elevenlabs_voice_id)."""

from __future__ import annotations

import asyncpg
import pytest
from aegis.db import run_migrations


@pytest.mark.asyncio
async def test_agents_elevenlabs_voice_id_column(db_pool: asyncpg.Pool) -> None:
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        voice_type = await conn.fetchval(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='agents' "
            "AND column_name='elevenlabs_voice_id'"
        )
    assert voice_type == "text", (
        f"agents.elevenlabs_voice_id expected data_type 'text', got {voice_type!r}"
    )
