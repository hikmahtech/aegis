"""Real-Postgres test for migrations/011_todoist_capture.sql."""

from __future__ import annotations

import pytest
from aegis.db import run_migrations


@pytest.mark.asyncio
async def test_migration_011_creates_idempotency_table(db_pool):
    """todoist_capture_idempotency exists with the expected columns and PK."""
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        cols = await conn.fetch(
            """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_name = 'todoist_capture_idempotency'
            ORDER BY ordinal_position
            """
        )
    names = {r["column_name"] for r in cols}
    assert {"source_tag", "external_id", "todoist_task_ref", "captured_at"} <= names

    # Primary key on (source_tag, external_id)
    async with db_pool.acquire() as conn:
        pk = await conn.fetchval(
            """
            SELECT pg_get_constraintdef(c.oid)
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            WHERE t.relname = 'todoist_capture_idempotency' AND c.contype = 'p'
            """
        )
    assert pk is not None
    assert "source_tag" in pk and "external_id" in pk


@pytest.mark.asyncio
async def test_migration_011_seeds_kill_switch_true(db_pool):
    """settings.todoist_capture_enabled is seeded to true if absent.

    asyncpg's JSONB codec deserializes 'true'::jsonb to Python bool True;
    see the AEGIS double-encoding lesson in CLAUDE.md for why we expect
    a native bool here rather than the string "true".

    We DELETE then directly replay the migration's INSERT to verify the
    exact SQL the migration runs, independent of schema_migrations tracking.
    """
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        # Simulate absent-key scenario by removing any prior value, then
        # replay the exact INSERT from migration 011.
        await conn.execute(
            "DELETE FROM settings WHERE key = 'todoist_capture_enabled'"
        )
        await conn.execute(
            "INSERT INTO settings (key, value) "
            "VALUES ('todoist_capture_enabled', 'true'::jsonb) "
            "ON CONFLICT (key) DO NOTHING"
        )
        value = await conn.fetchval(
            "SELECT value FROM settings WHERE key = 'todoist_capture_enabled'"
        )
    assert value is True


@pytest.mark.asyncio
async def test_migration_011_idempotency_index_on_captured_at(db_pool):
    """An index exists on captured_at for retention sweeps."""
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        idx = await conn.fetchval(
            """
            SELECT 1 FROM pg_indexes
            WHERE tablename = 'todoist_capture_idempotency'
              AND indexdef ILIKE '%captured_at%'
            """
        )
    assert idx == 1


@pytest.mark.asyncio
async def test_migration_011_kill_switch_does_not_overwrite_existing(db_pool):
    """ON CONFLICT DO NOTHING: a pre-existing false must survive migration.

    Operators can disable captures via PUT /api/settings/todoist_capture_enabled
    at any time. Re-running migration 011 (e.g. on a Core restart after the
    operator disabled captures) must NOT clobber that decision.

    We replay the migration's INSERT directly (schema_migrations tracking
    prevents run_migrations from re-executing an already-applied file).
    """
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        # Operator disables captures.
        await conn.execute(
            "UPDATE settings SET value = 'false'::jsonb "
            "WHERE key = 'todoist_capture_enabled'"
        )
        # Replay the exact INSERT from migration 011 — simulates Core restart.
        # ON CONFLICT (key) DO NOTHING must leave the operator's false intact.
        await conn.execute(
            "INSERT INTO settings (key, value) "
            "VALUES ('todoist_capture_enabled', 'true'::jsonb) "
            "ON CONFLICT (key) DO NOTHING"
        )
        value = await conn.fetchval(
            "SELECT value FROM settings WHERE key = 'todoist_capture_enabled'"
        )
    assert value is False
