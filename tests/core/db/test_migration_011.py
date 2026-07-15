import pytest
from aegis.db.pool import run_migrations


@pytest.mark.asyncio
async def test_triage_accuracy_has_last_checked_at(db_pool):
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        col = await conn.fetchval(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='triage_accuracy' AND column_name='last_checked_at'"
        )
    assert col == "last_checked_at"
