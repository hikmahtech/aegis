"""AgentTaskFlow's `research` verb on a TOPIC task (#513 validation).

A topic round's task is titled "<topic>: new items worth a look". Researched
verbatim, that cost a smart-tier run to answer nothing. The run researches the
topic, with the round's items as context and their links read first."""

from __future__ import annotations

import uuid

import pytest_asyncio
from temporalio import activity

from tests.worker.flows.test_agent_task_research import (
    _DESCRIPTION,
    _TITLE,
    _research_activities,
    _run,
)

_ITEMS = [
    {"title": "Agents ship to production", "url": "https://news.example/agents"},
    {"title": "A new agent benchmark", "url": "https://news.example/bench"},
]


@pytest_asyncio.fixture(loop_scope="function")
async def topic_task(db_pool):
    task_id = f"tt-{uuid.uuid4().hex[:8]}"
    await db_pool.execute(
        "INSERT INTO todoist_tasks "
        "(id, content, description, labels, source_tag, assignee_label, is_completed) "
        "VALUES ($1, $2, $3, ARRAY['#research', '@raphael'], '#research', '@raphael', false)",
        task_id,
        _TITLE,
        _DESCRIPTION,
    )
    yield task_id
    await db_pool.execute("DELETE FROM todoist_tasks WHERE id = $1", task_id)
    await db_pool.execute("DELETE FROM todoist_outbox WHERE temp_id = $1", f"agent-task-park-{task_id}")


async def test_a_topic_task_researches_the_topic_not_its_title(db_pool, topic_task):
    calls: list = []

    @activity.defn(name="research_task_problem")
    async def topic_problem(task_id: str) -> dict:
        return {"problem_id": "p-1", "class": "topic", "topic": "AI agents", "items": _ITEMS}

    acts = [topic_problem, *_research_activities(calls)[1:]]
    result = await _run(db_pool, topic_task, acts)

    _, question, context = next(c for c in calls if c[0] == "synth")
    assert "AI agents" in question
    assert _TITLE not in question
    assert "Agents ship to production" in context
    gather = next(c for c in calls if c[0] == "gather")[1]
    assert gather["seed_urls"][:2] == [i["url"] for i in _ITEMS]
    assert result["verb"] == "research" and result["status"] == "answered"


async def test_an_ordinary_research_task_still_asks_its_title(db_pool, topic_task):
    calls: list = []
    result = await _run(db_pool, topic_task, _research_activities(calls))
    _, question, _ = next(c for c in calls if c[0] == "synth")
    assert question == _TITLE
    assert result["status"] == "answered"
