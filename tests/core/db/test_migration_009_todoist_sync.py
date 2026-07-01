"""Real-Postgres test for migrations/009_todoist_sync.sql.

Reuses tests/core/db/conftest.py db_pool fixture pointing at
localhost:25432. Skips when no Postgres is reachable.
"""

from __future__ import annotations

import pytest
from aegis.db import run_migrations

EXPECTED_NEW_TABLES = {
    "todoist_projects",
    "todoist_tasks",
    "todoist_labels",
    "todoist_sync_state",
    "todoist_outbox",
    "todoist_webhook_events",
}


@pytest.mark.asyncio
async def test_migration_009_creates_projection_tables(db_pool):
    """All Todoist projection tables exist after running migrations."""
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
    names = {r["tablename"] for r in rows}
    missing = EXPECTED_NEW_TABLES - names
    assert not missing, f"missing tables after migration: {missing}"


@pytest.mark.asyncio
async def test_workflow_runs_has_todoist_task_ref_column(db_pool):
    """workflow_runs gains a nullable todoist_task_ref TEXT column with index."""
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        col = await conn.fetchrow(
            """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_name = 'workflow_runs' AND column_name = 'todoist_task_ref'
            """
        )
        idx = await conn.fetchval(
            """
            SELECT 1 FROM pg_indexes
            WHERE tablename = 'workflow_runs' AND indexdef ILIKE '%todoist_task_ref%'
            """
        )
    assert col is not None, "todoist_task_ref column missing"
    assert col["data_type"] == "text"
    assert col["is_nullable"] == "YES"
    assert idx == 1, "expected index on workflow_runs(todoist_task_ref)"


@pytest.mark.asyncio
async def test_todoist_sync_state_main_row_exists(db_pool):
    """Migration creates a 'main' row in todoist_sync_state. Sync_token value
    may be '*' (fresh DB) or any string (after worker has run sync). What
    matters for the migration's correctness is that the row exists."""
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT key, sync_token FROM todoist_sync_state WHERE key = 'main'"
        )
    assert row is not None
    assert row["key"] == "main"
    assert row["sync_token"]  # any non-empty string is fine
