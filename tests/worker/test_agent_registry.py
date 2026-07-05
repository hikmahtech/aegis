"""Tests for AgentRegistryActivities.resolve_agents (issue #36 groundwork)."""

from __future__ import annotations

import pytest_asyncio
from aegis_worker.activities.agent_registry import AgentRegistryActivities
from temporalio.testing import ActivityEnvironment


@pytest_asyncio.fixture(loop_scope="function")
async def activities(db_pool):
    # Synthetic tags so pre-existing rows in the dev DB can never match.
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agents (id, name, role, system_prompt_path, capabilities, active)
            VALUES
                ('tagtest-a', 'A', 'test', '', '["tagtest-dup"]'::jsonb, TRUE),
                ('tagtest-b', 'B', 'test', '', '["tagtest-dup", "tagtest-solo"]'::jsonb, TRUE),
                ('tagtest-c', 'C', 'test', '', '["tagtest-inactive"]'::jsonb, FALSE)
            ON CONFLICT (id) DO NOTHING
            """
        )
    yield AgentRegistryActivities(db_pool=db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM agents WHERE id LIKE 'tagtest-%'")


async def test_resolve_single_match(activities):
    env = ActivityEnvironment()
    result = await env.run(activities.resolve_agents, ["tagtest-solo"])
    assert result == {"tagtest-solo": "tagtest-b"}


async def test_resolve_no_match_returns_none(activities):
    env = ActivityEnvironment()
    result = await env.run(activities.resolve_agents, ["tagtest-missing"])
    assert result == {"tagtest-missing": None}


async def test_resolve_ambiguous_first_id_wins(activities):
    env = ActivityEnvironment()
    result = await env.run(activities.resolve_agents, ["tagtest-dup"])
    assert result == {"tagtest-dup": "tagtest-a"}


async def test_resolve_excludes_inactive(activities):
    env = ActivityEnvironment()
    result = await env.run(activities.resolve_agents, ["tagtest-inactive"])
    assert result == {"tagtest-inactive": None}


async def test_resolve_no_pool_returns_all_none():
    env = ActivityEnvironment()
    activities = AgentRegistryActivities(db_pool=None)
    result = await env.run(activities.resolve_agents, ["tagtest-solo", "tagtest-dup"])
    assert result == {"tagtest-solo": None, "tagtest-dup": None}
