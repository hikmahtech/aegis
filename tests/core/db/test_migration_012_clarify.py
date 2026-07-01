"""Schema-level assertions for migration 012 (Phase 3 clarify)."""

from __future__ import annotations

import asyncpg
import pytest
from aegis.db import run_migrations


async def _table_exists(conn: asyncpg.Connection, name: str) -> bool:
    return bool(
        await conn.fetchval(
            "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=$1",
            name,
        )
    )


async def _column_exists(conn: asyncpg.Connection, table: str, col: str) -> bool:
    return bool(
        await conn.fetchval(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=$1 AND column_name=$2",
            table,
            col,
        )
    )


@pytest.mark.asyncio
async def test_gtd_clarify_log_exists(db_pool: asyncpg.Pool) -> None:
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        assert await _table_exists(conn, "gtd_clarify_log")
        for col in (
            "id", "todoist_task_id", "pass", "source_tag", "classification",
            "confidence", "assignee", "contexts", "reason", "user_hint",
            "llm_model", "prompt_tokens", "completion_tokens", "latency_ms",
            "applied", "created_at",
        ):
            assert await _column_exists(conn, "gtd_clarify_log", col), f"missing {col}"


@pytest.mark.asyncio
async def test_todoist_notes_exists(db_pool: asyncpg.Pool) -> None:
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        assert await _table_exists(conn, "todoist_notes")
        for col in ("id", "item_id", "content", "posted_uid", "posted_at", "raw", "updated_at"):
            assert await _column_exists(conn, "todoist_notes", col), f"missing {col}"


@pytest.mark.asyncio
async def test_todoist_tasks_columns_added(db_pool: asyncpg.Pool) -> None:
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        assert await _column_exists(conn, "todoist_tasks", "last_clarified_at")
        assert await _column_exists(conn, "todoist_tasks", "last_note_at")


@pytest.mark.asyncio
async def test_settings_seeds_present(db_pool: asyncpg.Pool) -> None:
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        for key, expected in (
            ("gtd_clarify_enabled", True),
            ("gtd_2min_rule_enabled", True),
            ("user_timezone", "Asia/Kolkata"),
        ):
            value = await conn.fetchval("SELECT value FROM settings WHERE key=$1", key)
            assert value == expected, f"{key} seeded as {value!r}, expected {expected!r}"
