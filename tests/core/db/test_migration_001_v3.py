"""Real-Postgres test for migrations/001_v3_schema.sql.

Relies on the db_pool fixture in tests/core/db/conftest.py which
points at the local dev Postgres (postgresql://aegis:aegis_dev@localhost:25432/aegis).
Skips when no Postgres is reachable.
"""

from __future__ import annotations

import pytest
from aegis.db import run_migrations

EXPECTED_PUBLIC_TABLES = {
    "schema_migrations",
    "agents",
    "activities",
    "interactions",
    "workflow_runs",
    "settings",
    "channels",
    "resources",
    "llm_calls",
    "connector_calls",
    "chat_tool_calls",
    "audit_log",
    "chat_history",
    "knowledge_injection_log",
    "knowledge_source_quality",
    "triage_state",
    "triage_accuracy",
    "ingest_idempotency",
}

EXPECTED_MAOU_TABLES = {
    "recurring_charge",
    "receipt_email",
    "renewal_alert",
    "subscription_digest",
}

EXPECTED_PANDORAS_ACTOR_TABLES = {
    "homelab_drift",
    "backup_health",
    "schedule_health",
    "cert_expiry",
}


@pytest.mark.asyncio
async def test_all_public_tables_created(db_pool):
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public'")
    found = {r["tablename"] for r in rows}
    missing = EXPECTED_PUBLIC_TABLES - found
    assert not missing, f"missing public tables: {missing}"


@pytest.mark.asyncio
async def test_maou_schema_and_tables(db_pool):
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname='maou'")
    found = {r["tablename"] for r in rows}
    missing = EXPECTED_MAOU_TABLES - found
    assert not missing, f"missing maou tables: {missing}"


@pytest.mark.asyncio
async def test_pandoras_actor_schema_and_tables(db_pool):
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname='pandoras_actor'")
    found = {r["tablename"] for r in rows}
    missing = EXPECTED_PANDORAS_ACTOR_TABLES - found
    assert not missing, f"missing pandoras_actor tables: {missing}"


@pytest.mark.asyncio
async def test_interactions_has_expected_indexes(db_pool):
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT indexname FROM pg_indexes WHERE tablename='interactions'")
    names = {r["indexname"] for r in rows}
    # three non-PK indexes per spec §8
    assert any("agent_id" in n for n in names)
    assert any("flow_run_id" in n for n in names)
    assert any("origin" in n for n in names)


@pytest.mark.asyncio
async def test_workflow_runs_has_running_partial_index(db_pool):
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename='workflow_runs' AND indexdef ILIKE '%status = ''running''%'"
        )
    assert row is not None, "missing partial index on workflow_runs(status) WHERE status='running'"


@pytest.mark.asyncio
async def test_001_v3_recorded_in_schema_migrations(db_pool):
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT filename FROM schema_migrations WHERE filename='001_v3_schema.sql'"
        )
    assert row is not None
