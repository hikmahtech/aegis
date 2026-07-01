import pytest
from aegis.db import run_migrations


@pytest.mark.asyncio
async def test_homelab_tables_live_in_pandoras_actor_schema(db_pool):
    """After v3 migration, all four homelab tables live under pandoras_actor."""
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        tables = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname='pandoras_actor' "
            "AND tablename IN "
            "('homelab_drift','backup_health','schedule_health','cert_expiry')"
        )
    assert len(tables) == 4
