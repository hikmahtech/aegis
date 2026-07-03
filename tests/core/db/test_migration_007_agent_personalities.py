"""Schema assertions for migration 007 (agent_personalities table)."""

from __future__ import annotations

import asyncpg
import pytest
from aegis.db import run_migrations


@pytest.mark.asyncio
async def test_migration_007_schema(db_pool: asyncpg.Pool) -> None:
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        cols = {
            r["column_name"]: r["data_type"]
            for r in await conn.fetch(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='agent_personalities'"
            )
        }
        assert cols.get("agent_id") == "text"
        assert cols.get("kind") == "text"
        assert cols.get("content") == "text"
        assert cols.get("updated_at") == "timestamp with time zone"

        # kind is CHECK-constrained to the four persona kinds.
        for kind in ("soul", "agents", "user", "memory"):
            ok = await conn.fetchval(
                "SELECT count(*) FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
                "WHERE t.relname = 'agent_personalities' AND c.contype = 'c' "
                "AND pg_get_constraintdef(c.oid) LIKE '%' || $1 || '%'",
                kind,
            )
            assert ok >= 1, f"kind CHECK constraint missing '{kind}'"

        # The interim agents persona columns are gone (content backfilled).
        agents_cols = {
            r["column_name"]
            for r in await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='agents'"
            )
        }
        assert not {"soul", "operating_notes", "user_context"} & agents_cols


@pytest.mark.asyncio
async def test_migration_007_kind_check_enforced(db_pool: asyncpg.Pool) -> None:
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM agents WHERE id = 'zz-mig007'")
        await conn.execute(
            "INSERT INTO agents (id, name, role, system_prompt_path, active) "
            "VALUES ('zz-mig007', 'Z', 'r', '', true)"
        )
        try:
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "INSERT INTO agent_personalities (agent_id, kind, content) "
                    "VALUES ('zz-mig007', 'not-a-kind', 'x')"
                )
        finally:
            await conn.execute("DELETE FROM agents WHERE id = 'zz-mig007'")
