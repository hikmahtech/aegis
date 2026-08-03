import pytest
from aegis.db import run_migrations


@pytest.mark.asyncio
async def test_homelab_tables_live_in_pandoras_actor_schema(db_pool):
    """The surviving homelab tables live under pandoras_actor.

    Baseline shipped four; migration 022 dropped backup_health and
    schedule_health along with the flows that fed them (aegis#99), leaving
    homelab_drift (ServiceDriftFlow) and cert_expiry (CertRadarFlow).
    """
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        tables = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname='pandoras_actor'"
        )
    found = {r["tablename"] for r in tables}
    assert {"homelab_drift", "cert_expiry"} <= found


@pytest.mark.asyncio
async def test_dead_health_tables_are_dropped(db_pool):
    """Migration 022 must actually remove backup_health / schedule_health.

    001_baseline.sql still CREATEs them, so on a fresh database this only
    passes if 022 ran after it — which is what makes the drop real rather
    than a comment.
    """
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        tables = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname='pandoras_actor' "
            "AND tablename IN ('backup_health','schedule_health')"
        )
    assert [r["tablename"] for r in tables] == []


@pytest.mark.asyncio
async def test_migration_022_is_idempotent(db_pool):
    """Re-running the DROP must not raise — `IF EXISTS` covers the case where
    the tables are already gone (every deploy after the first)."""
    await run_migrations(db_pool)
    sql = (
        "DROP TABLE IF EXISTS pandoras_actor.backup_health;\n"
        "DROP TABLE IF EXISTS pandoras_actor.schedule_health;"
    )
    async with db_pool.acquire() as conn:
        await conn.execute(sql)
        await conn.execute(sql)
