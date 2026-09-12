"""AgentTaskFlow, `research` verb (#509): a `#research` task given to Raphael
is researched and answered on the task, instead of going to `ask`.

The task side runs on the real `AgentTaskActivities` and the real database
(verb resolution, the park); ResearchFlow runs for real as the child, with its
steps stubbed; the comment is captured so the posted answer can be read.
"""

from __future__ import annotations

import uuid

import pytest_asyncio
from aegis_worker.activities.agent_task import PARK_LABEL, AgentTaskActivities
from aegis_worker.flows.agent_task import AgentTaskFlow, AgentTaskFlowInput
from aegis_worker.flows.research import ResearchFlow
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

_TITLE = "Why Does Everyone Hate AI? - Hacker News"
_DESCRIPTION = "[Read](https://news.example/story)\n\nA thread.\n\nWhy: people are worried"


def _research_activities(calls: list, *, synthesized: bool = True):
    @activity.defn(name="research_task_problem")
    async def task_problem(task_id: str) -> dict:
        calls.append(("problem", task_id))
        return {"problem_id": "p-1"}

    @activity.defn(name="research_gather")
    async def gather(request: dict) -> dict:
        calls.append(("gather", request))
        return {"kg": [], "web": [], "papers": [], "to_read": list(request["seed_urls"]), "errors": []}

    @activity.defn(name="research_read")
    async def read(urls: list) -> dict:
        return {"pages": [{"url": u, "title": "The story", "text": "body"} for u in urls], "errors": []}

    @activity.defn(name="research_synthesize")
    async def synthesize(question: str, context: str, gathered: dict, pages: list) -> dict:
        calls.append(("synth", question, context))
        if not synthesized:
            return {"answer": "I found nothing on this.", "synthesized": False, "sources": []}
        return {
            "answer": "Mostly jobs and slop [1].",
            "synthesized": True,
            "sources": [{"n": 1, "kind": "page", "title": "The story", "url": pages[0]["url"]}],
        }

    @activity.defn(name="research_save")
    async def save(question: str, answer: str, sources: list) -> dict:
        calls.append(("save", question))
        return {"saved": True}

    @activity.defn(name="comment")
    async def comment(task_id: str, agent_id: str, body: str) -> dict:
        calls.append(("comment", task_id, agent_id, body))
        return {"ok": True}

    return [task_problem, gather, read, synthesize, save, comment]


@pytest_asyncio.fixture(loop_scope="function")
async def research_task(db_pool):
    task_id = f"tr-{uuid.uuid4().hex[:8]}"
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


async def _run(db_pool, task_id: str, activities: list) -> dict:
    act = AgentTaskActivities(db_pool=db_pool)
    task = dict(await act.load_task(task_id))
    task.pop("notes", None)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"tq-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[AgentTaskFlow, ResearchFlow],
            activities=[act.load_task_context, act.park_task, *activities],
        ):
            return await env.client.execute_workflow(
                AgentTaskFlow.run,
                AgentTaskFlowInput(agent_id="raphael", todoist_task_id=task_id, task=task),
                id=f"agent-task-{task_id}",
                task_queue=queue,
            )


async def test_a_research_task_is_researched_and_answered_on_the_task(db_pool, research_task):
    """#344's one real `#research` case in prod parked with nothing done; under
    `ask` the agent could only chat about it. Now it gets an answer."""
    calls: list = []
    result = await _run(db_pool, research_task, _research_activities(calls))

    assert result == {
        "task_id": research_task,
        "verb": "research",
        "status": "answered",
        "sources": 1,
        "saved": True,
    }
    assert ("problem", research_task) in calls
    gather = next(c for c in calls if c[0] == "gather")[1]
    # The title is the question; the description's link is read first.
    assert gather["question"] == _TITLE
    assert gather["seed_urls"] == ["https://news.example/story"]
    synth = next(c for c in calls if c[0] == "synth")
    assert "people are worried" in synth[2]
    comments = [c for c in calls if c[0] == "comment"]
    assert len(comments) == 1
    _, task_ref, agent, body = comments[0]
    assert (task_ref, agent) == (research_task, "raphael")
    assert body == "Mostly jobs and slop [1].\n\nSources:\n[1] The story — https://news.example/story"
    labels = await db_pool.fetchval("SELECT labels FROM todoist_tasks WHERE id = $1", research_task)
    assert PARK_LABEL in labels


async def test_no_answer_is_still_said_on_the_task_and_parks(db_pool, research_task):
    calls: list = []
    result = await _run(db_pool, research_task, _research_activities(calls, synthesized=False))
    assert result["status"] == "no_answer"
    assert result["saved"] is False
    assert not [c for c in calls if c[0] == "save"]
    body = next(c for c in calls if c[0] == "comment")[3]
    assert body == "I found nothing on this."
    labels = await db_pool.fetchval("SELECT labels FROM todoist_tasks WHERE id = $1", research_task)
    assert PARK_LABEL in labels
