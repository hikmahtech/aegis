# tests/core/test_alert_governance_migration.py
"""Smoke test: both tables from migration 002 exist on a fresh DB."""

import pytest
from aegis.db import run_migrations


@pytest.mark.asyncio
async def test_alert_governance_tables_exist(db_pool):
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        alert_mutes = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='alert_mutes')"
        )
        pending_prs = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='pending_prs')"
        )
    assert alert_mutes is True
    assert pending_prs is True
