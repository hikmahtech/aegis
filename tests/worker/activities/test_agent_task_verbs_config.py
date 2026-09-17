"""The agent-task lane reads `agent_task_verbs` through the core service (#558).

What the admin Todoist page saves (`save_agent_task_verbs`, strict) is exactly
what the lane routes by (`load_verbs`, lenient) — one table, one merge.
"""

from __future__ import annotations

import pytest_asyncio
from aegis.services import agent_task_verbs as core_verbs
from aegis_worker.activities import agent_task
from aegis_worker.activities.agent_task import load_verbs, resolve_verb


def test_the_worker_table_is_the_core_table():
    assert agent_task.DEFAULT_VERBS is core_verbs.DEFAULT_VERBS
    assert agent_task.VERBS is core_verbs.VERBS
    assert agent_task.merge_verbs is core_verbs.merge
    assert agent_task.VERBS_SETTING == core_verbs.SETTINGS_KEY


@pytest_asyncio.fixture(loop_scope="function")
async def clean(db_pool):
    await db_pool.execute("DELETE FROM settings WHERE key = 'agent_task_verbs'")
    yield db_pool
    await db_pool.execute("DELETE FROM settings WHERE key = 'agent_task_verbs'")


async def test_a_table_saved_on_the_admin_page_reroutes_the_lane(clean):
    pool = clean
    assert resolve_verb({"source_tag": "#chat"}, await load_verbs(pool)) == "ask"

    await core_verbs.save_agent_task_verbs(pool, {"#calendar": None, "#chat": "research"})
    verbs = await load_verbs(pool)
    assert resolve_verb({"source_tag": "#calendar"}, verbs) == "none"
    assert resolve_verb({"source_tag": "#chat"}, verbs) == "research"
    assert resolve_verb({"source_tag": "#alert"}, verbs) == "infra"  # untouched default

    await core_verbs.save_agent_task_verbs(pool, {})
    assert await load_verbs(pool) == core_verbs.DEFAULT_VERBS
