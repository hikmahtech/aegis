# tests/core/test_alert_governance_migration.py
"""Smoke test: `pending_prs` exists on a fresh DB and `alert_mutes` does not.

Migration 033 (problem hub, PR 4a) dropped `alert_mutes`: mutes are
`problems.muted_until`. This pins the drop so a stray reference cannot come
back without noticing.
"""

import pytest
from aegis.db import run_migrations


@pytest.mark.asyncio
async def test_alert_governance_tables(db_pool):
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        alert_mutes = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='alert_mutes')"
        )
        pending_prs = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='pending_prs')"
        )
    assert alert_mutes is False
    assert pending_prs is True
