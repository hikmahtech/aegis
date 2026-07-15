"""Real-Postgres tests for migrations/010_leadfactory.sql."""

from __future__ import annotations

import asyncpg
import pytest
import pytest_asyncio
from aegis.db import run_migrations

CLIENT_SLUG = "lftest-client"


async def _purge(conn) -> None:
    """Remove this test's rows (idempotent across runs).

    lead_events is append-only by trigger; cleanup disables it briefly —
    the connection is the table owner, so no superuser needed.
    """
    await conn.execute(
        "ALTER TABLE leadfactory.lead_events DISABLE TRIGGER lead_events_append_only"
    )
    try:
        for sql in (
            """DELETE FROM leadfactory.lead_events e USING leadfactory.leads l,
               leadfactory.clients c
               WHERE e.lead_id = l.id AND l.client_id = c.id AND c.slug = $1""",
            """DELETE FROM leadfactory.messages m USING leadfactory.leads l,
               leadfactory.clients c
               WHERE m.lead_id = l.id AND l.client_id = c.id AND c.slug = $1""",
            """DELETE FROM leadfactory.leads l USING leadfactory.clients c
               WHERE l.client_id = c.id AND c.slug = $1""",
            """DELETE FROM leadfactory.projects p USING leadfactory.clients c
               WHERE p.client_id = c.id AND c.slug = $1""",
            """DELETE FROM leadfactory.digest_log d USING leadfactory.clients c
               WHERE d.client_id = c.id AND c.slug = $1""",
            "DELETE FROM leadfactory.clients WHERE slug = $1",
        ):
            await conn.execute(sql, CLIENT_SLUG)
    finally:
        await conn.execute(
            "ALTER TABLE leadfactory.lead_events ENABLE TRIGGER lead_events_append_only"
        )


# loop_scope must match db_pool's — a session-loop fixture using the
# function-loop pool trips asyncpg's "another operation is in progress".
@pytest_asyncio.fixture(loop_scope="function")
async def lf_pool(db_pool):
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        await _purge(conn)
    yield db_pool
    async with db_pool.acquire() as conn:
        await _purge(conn)


async def _seed_lead(conn) -> tuple[int, int]:
    """Insert client -> project -> lead; returns (client_id, lead_id)."""
    client_id = await conn.fetchval(
        """INSERT INTO leadfactory.clients (slug, name, domain)
           VALUES ($1, 'LF Test Client', 'lftest.example')
           RETURNING id""",
        CLIENT_SLUG,
    )
    project_id = await conn.fetchval(
        """INSERT INTO leadfactory.projects
             (client_id, slug, name, rera_no, locality, configs,
              price_min_lakh, price_max_lakh)
           VALUES ($1, 'lftest-towers', 'LF Test Towers', 'P99999999999',
                   'Kharghar', '{1BHK,2BHK}', 50, 60)
           RETURNING id""",
        client_id,
    )
    lead_id = await conn.fetchval(
        """INSERT INTO leadfactory.leads
             (client_id, project_id, phone, name, source, meta_campaign_id,
              meta_adset_id, next_action, next_action_at)
           VALUES ($1, $2, '+919800000210', 'Rakesh Test', 'meta_leadform',
                   'camp-1', 'adset-kharghar', 'send_T1', now())
           RETURNING id""",
        client_id,
        project_id,
    )
    return client_id, lead_id


@pytest.mark.asyncio
async def test_migration_010_creates_leadfactory_schema(db_pool):
    """All six tables and both views exist in the leadfactory schema."""
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        tables = {
            r["table_name"]
            for r in await conn.fetch(
                """SELECT table_name FROM information_schema.tables
                   WHERE table_schema = 'leadfactory'"""
            )
        }
        views = {
            r["table_name"]
            for r in await conn.fetch(
                """SELECT table_name FROM information_schema.views
                   WHERE table_schema = 'leadfactory'"""
            )
        }
    assert {
        "clients", "projects", "leads", "messages", "lead_events", "digest_log",
    } <= tables
    assert {"v_funnel_by_adset", "v_response_times"} <= views


@pytest.mark.asyncio
async def test_migration_010_seeds_kill_switch_false(db_pool):
    """settings.leadfactory_enabled is seeded to false if absent."""
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM settings WHERE key = 'leadfactory_enabled'")
        await conn.execute(
            "INSERT INTO settings (key, value) "
            "VALUES ('leadfactory_enabled', 'false'::jsonb) "
            "ON CONFLICT (key) DO NOTHING"
        )
        value = await conn.fetchval(
            "SELECT value FROM settings WHERE key = 'leadfactory_enabled'"
        )
    assert value is False


@pytest.mark.asyncio
async def test_leads_constraints(lf_pool):
    """Duplicate (client, phone), bad state, and bad source are all rejected."""
    async with lf_pool.acquire() as conn:
        client_id, _ = await _seed_lead(conn)

        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                """INSERT INTO leadfactory.leads (client_id, phone, source)
                   VALUES ($1, '+919800000210', 'manual')""",
                client_id,
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                """INSERT INTO leadfactory.leads (client_id, phone, source, state)
                   VALUES ($1, '+919800000211', 'manual', 'LIMBO')""",
                client_id,
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                """INSERT INTO leadfactory.leads (client_id, phone, source)
                   VALUES ($1, '+919800000212', 'carrier_pigeon')""",
                client_id,
            )


@pytest.mark.asyncio
async def test_lead_events_append_only(lf_pool):
    """UPDATE and DELETE on the diary raise; INSERT succeeds."""
    async with lf_pool.acquire() as conn:
        _, lead_id = await _seed_lead(conn)
        event_id = await conn.fetchval(
            """INSERT INTO leadfactory.lead_events (lead_id, actor, event)
               VALUES ($1, 'machine', 'created') RETURNING id""",
            lead_id,
        )
        with pytest.raises(asyncpg.PostgresError, match="append-only"):
            await conn.execute(
                "UPDATE leadfactory.lead_events SET actor = 'buyer' WHERE id = $1",
                event_id,
            )
        with pytest.raises(asyncpg.PostgresError, match="append-only"):
            await conn.execute(
                "DELETE FROM leadfactory.lead_events WHERE id = $1", event_id
            )


@pytest.mark.asyncio
async def test_views_compute_response_time_and_funnel(lf_pool):
    """v_response_times measures first outbound reply; v_funnel_by_adset counts
    ever-reached milestones from the diary, not current state."""
    async with lf_pool.acquire() as conn:
        client_id, lead_id = await _seed_lead(conn)

        await conn.execute(
            """INSERT INTO leadfactory.messages (lead_id, direction, template, body, at)
               SELECT $1, 'out', 'T1', 'Hi Rakesh!', created_at + interval '41 seconds'
               FROM leadfactory.leads WHERE id = $1""",
            lead_id,
        )
        # Later outbound must not change first_response.
        await conn.execute(
            """INSERT INTO leadfactory.messages (lead_id, direction, body, at)
               SELECT $1, 'out', 'follow-up', created_at + interval '1 day'
               FROM leadfactory.leads WHERE id = $1""",
            lead_id,
        )
        first = await conn.fetchval(
            "SELECT first_response FROM leadfactory.v_response_times WHERE lead_id = $1",
            lead_id,
        )
        assert first.total_seconds() == 41

        # Lead qualified then closed lost — still qualified in its cohort.
        await conn.execute(
            """INSERT INTO leadfactory.lead_events (lead_id, actor, event, detail)
               VALUES ($1, 'machine', 'state_change',
                       '{"from": "QUALIFYING", "to": "QUALIFIED"}'),
                      ($1, 'broker', 'visit_booked', NULL),
                      ($1, 'machine', 'state_change',
                       '{"from": "VISIT_BOOKED", "to": "CLOSED_LOST"}')""",
            lead_id,
        )
        await conn.execute(
            "UPDATE leadfactory.leads SET state = 'CLOSED_LOST', next_action_at = NULL "
            "WHERE id = $1",
            lead_id,
        )
        row = await conn.fetchrow(
            """SELECT * FROM leadfactory.v_funnel_by_adset
               WHERE client_id = $1 AND meta_adset_id = 'adset-kharghar'""",
            client_id,
        )
    assert row["leads"] == 1
    assert row["qualified"] == 1
    assert row["visits_booked"] == 1
    assert row["visits_done"] == 0
