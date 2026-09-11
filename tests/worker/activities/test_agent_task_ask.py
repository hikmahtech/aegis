"""The `ask` verb's input (#344): which agent to ask about a task, and what.

Real database throughout: the agent is found through the `agents` table
(`metadata.mention_aliases`), the same registry clarify's comment channel
reads, so nothing in the lane names an agent.
"""

from __future__ import annotations

import pytest_asyncio
from aegis_worker.activities import clarify as _clarify
from aegis_worker.activities.agent_task import AgentTaskActivities

_AGENT = "asktest-agent"
_LABEL = "@asktest"


@pytest_asyncio.fixture(loop_scope="function")
async def _seed(db_pool):
    await db_pool.execute("DELETE FROM todoist_notes WHERE item_id LIKE 'ak-%'")
    await db_pool.execute("DELETE FROM todoist_tasks WHERE id LIKE 'ak-%'")
    await db_pool.execute("DELETE FROM agents WHERE id = $1", _AGENT)
    await db_pool.execute(
        "INSERT INTO agents (id, name, role, system_prompt_path, active, metadata) "
        "VALUES ($1, 'Ask Test', 'tester', '', true, $2)",
        _AGENT,
        {"mention_aliases": ["asktest"]},
    )
    await db_pool.execute(
        "INSERT INTO todoist_tasks (id, content, description, labels, source_tag, "
        "assignee_label, is_completed) VALUES "
        "('ak-1', 'Why is the cache slow?', 'Started after the upgrade.', "
        " ARRAY['#chat', $1], '#chat', $1, false),"
        "('ak-2', 'Look into it', '', ARRAY['#manual', '@nobody-here'], '#manual', "
        " '@nobody-here', false)",
        _LABEL,
    )
    await db_pool.execute(
        "INSERT INTO todoist_notes (id, item_id, content, posted_at) VALUES "
        "('ak-n1', 'ak-1', 'It only happens on Mondays.', now())"
    )
    # The registry is cached for 30s per process; a stale cache from another
    # test would hide the agent inserted above.
    _clarify._agent_reg_cache.update(reg=None, ts=0.0)
    yield
    await db_pool.execute("DELETE FROM todoist_notes WHERE item_id LIKE 'ak-%'")
    await db_pool.execute("DELETE FROM todoist_tasks WHERE id LIKE 'ak-%'")
    await db_pool.execute("DELETE FROM agents WHERE id = $1", _AGENT)
    _clarify._agent_reg_cache.update(reg=None, ts=0.0)


async def test_the_task_goes_to_the_agent_its_label_names(db_pool, _seed):
    ask = await AgentTaskActivities(db_pool=db_pool).prepare_agent_ask("ak-1")
    assert ask["agent_id"] == _AGENT
    # The same thread clarify's comment channel uses, so a later reply on the
    # task continues this conversation instead of starting a new one.
    assert ask["thread_id"] == "todoist-task-ak-1"
    message = ask["message"]
    assert "Why is the cache slow?" in message
    assert "Started after the upgrade." in message
    assert "It only happens on Mondays." in message


async def test_the_first_turn_is_read_only(db_pool, _seed):
    """Nobody is in this conversation when the sweep asks, so the turn may look
    and answer but not change anything — the coding lane's turn-1 rule. A
    change waits for the user to reply on the task, which reaches the agent
    through clarify's comment channel."""
    message = (await AgentTaskActivities(db_pool=db_pool).prepare_agent_ask("ak-1"))["message"]
    lowered = message.lower()
    assert "read-only" in lowered
    assert "do not restart" in lowered
    assert "reply on the task" in lowered


async def test_a_label_no_agent_answers_to_is_parked_with_what_to_do(db_pool, _seed):
    ask = await AgentTaskActivities(db_pool=db_pool).prepare_agent_ask("ak-2")
    assert ask["agent_id"] == ""
    assert "@nobody-here" in ask["comment"]
    assert ask["reason"]


async def test_an_unknown_task_asks_nobody(db_pool, _seed):
    ask = await AgentTaskActivities(db_pool=db_pool).prepare_agent_ask("ak-absent")
    assert ask["agent_id"] == ""
    assert ask["reason"]
