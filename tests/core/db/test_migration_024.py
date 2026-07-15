import pytest
from aegis.db import run_migrations


@pytest.mark.asyncio
async def test_finance_schema_and_tables_created(db_pool):
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        tables = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname='finance' "
            "AND tablename IN "
            "('recurring_charge','receipt_email','renewal_alert','subscription_digest')"
        )
    assert len(tables) == 4
