"""_exec_create_schedule: NL-authored schedules -> activities row (chat tool).

Also covers the pre-existing _exec_query_activities bug found during planning:
it selected `name`/`last_run_at`, neither of which exists on `activities`
(schema: id, slug, workflow_type, agent_id, schedule_cron, config, active,
created_at, updated_at) -> UndefinedColumnError at runtime.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from aegis.services.chat import ToolContext, _exec_create_schedule, _exec_query_activities

pytestmark = pytest.mark.asyncio


def _fake_ctx(agent_id: str = "test-sched-agent") -> ToolContext:
    return ToolContext(agent_id=agent_id)


@pytest_asyncio.fixture(loop_scope="function")
async def seeded_agent(db_pool):
    """A throwaway agent to own test-created schedules."""
    await db_pool.execute("DELETE FROM activities WHERE agent_id = 'test-sched-agent'")
    await db_pool.execute("DELETE FROM agents WHERE id = 'test-sched-agent'")
    await db_pool.execute(
        "INSERT INTO agents (id, name, role, system_prompt_path) "
        "VALUES ('test-sched-agent', 'Test Sched Agent', 'r', '')"
    )
    yield
    await db_pool.execute("DELETE FROM activities WHERE agent_id = 'test-sched-agent'")
    await db_pool.execute("DELETE FROM agents WHERE id = 'test-sched-agent'")


async def test_query_activities_uses_real_columns(db_pool):
    """D0: today this raises asyncpg.exceptions.UndefinedColumnError."""
    out = json.loads(await _exec_query_activities(db_pool, {"active_only": False}, _fake_ctx()))
    assert isinstance(out, list)
    assert out  # seed data ships activities rows
    row = out[0]
    assert "slug" in row and "workflow_type" in row and "schedule_cron" in row


async def test_create_schedule_happy_path(db_pool, seeded_agent):
    await db_pool.execute("DELETE FROM activities WHERE slug = 'nl-test-briefing'")
    args = {
        "workflow_type": "DailyBriefingFlow",
        "cron": "0 7 * * *",
        "slug": "nl-test-briefing",
    }
    out = json.loads(await _exec_create_schedule(db_pool, args, _fake_ctx()))
    assert out["created"]["slug"] == "nl-test-briefing"
    row = await db_pool.fetchrow("SELECT * FROM activities WHERE slug='nl-test-briefing'")
    assert row is not None
    assert row["active"] and row["schedule_cron"] == "0 7 * * *"
    assert row["config"]["created_by"] == "chat"
    await db_pool.execute("DELETE FROM activities WHERE slug = 'nl-test-briefing'")


async def test_create_schedule_auto_derives_slug(db_pool, seeded_agent):
    args = {"workflow_type": "DailyBriefingFlow", "cron": "0 8 * * *"}
    out = json.loads(await _exec_create_schedule(db_pool, args, _fake_ctx()))
    slug = out["created"]["slug"]
    assert slug.startswith("nl-dailybriefingflow-")
    await db_pool.execute("DELETE FROM activities WHERE slug = $1", slug)


async def test_create_schedule_rejects_unknown_workflow_type(db_pool, seeded_agent):
    out = json.loads(
        await _exec_create_schedule(
            db_pool, {"workflow_type": "NopeFlow", "cron": "0 7 * * *"}, _fake_ctx()
        )
    )
    assert "error" in out
    assert "valid types" in out["error"].lower()


async def test_create_schedule_rejects_subfive_minute_cron(db_pool, seeded_agent):
    for cron in ("* * * * *", "*/2 * * * *", "bad cron"):
        out = json.loads(
            await _exec_create_schedule(
                db_pool,
                {"workflow_type": "DailyBriefingFlow", "cron": cron},
                _fake_ctx(),
            )
        )
        assert "error" in out, cron


async def test_create_schedule_allows_five_minute_cron(db_pool, seeded_agent):
    await db_pool.execute("DELETE FROM activities WHERE slug = 'nl-test-5min'")
    out = json.loads(
        await _exec_create_schedule(
            db_pool,
            {
                "workflow_type": "DailyBriefingFlow",
                "cron": "*/5 * * * *",
                "slug": "nl-test-5min",
            },
            _fake_ctx(),
        )
    )
    assert "created" in out
    await db_pool.execute("DELETE FROM activities WHERE slug = 'nl-test-5min'")


async def test_create_schedule_duplicate_slug(db_pool, seeded_agent):
    await db_pool.execute("DELETE FROM activities WHERE slug = 'nl-test-dup'")
    args = {
        "workflow_type": "DailyBriefingFlow",
        "cron": "0 7 * * *",
        "slug": "nl-test-dup",
    }
    first = json.loads(await _exec_create_schedule(db_pool, args, _fake_ctx()))
    assert "created" in first
    second = json.loads(await _exec_create_schedule(db_pool, args, _fake_ctx()))
    assert "error" in second
    assert "already exists" in second["error"]
    await db_pool.execute("DELETE FROM activities WHERE slug = 'nl-test-dup'")


async def test_create_schedule_unknown_agent_returns_friendly_error(db_pool):
    await db_pool.execute("DELETE FROM agents WHERE id = 'nonexistent-test-agent'")
    await db_pool.execute("DELETE FROM activities WHERE slug = 'nl-test-noagent'")
    args = {
        "workflow_type": "DailyBriefingFlow",
        "cron": "0 7 * * *",
        "slug": "nl-test-noagent",
    }
    out = json.loads(
        await _exec_create_schedule(db_pool, args, _fake_ctx(agent_id="nonexistent-test-agent"))
    )
    assert "error" in out
    assert "not found" in out["error"]
