"""A problem's task is assigned by capability tag, never a literal agent id
(issue #36): when no active agent holds the owner's tag, the task is created
with no assignee and its GTD state label alone (#139), and the miss is logged."""

from __future__ import annotations

import uuid

import pytest_asyncio
from aegis.services.hub import TASK_SUBJECT_KIND, Event, ingest_event
from aegis.services.hub_project import project

from tests.core.test_hub_project import NOW, _cmds, inbox, todoist  # noqa: F401 — fixtures


def _question() -> Event:
    task = f"zzq-{uuid.uuid4().hex[:8]}"
    return Event(
        source="research",
        external_id=f"task-{task}",
        kind="occurrence",
        title="What is new in RAG?",
        klass="question",
        subject=f"task-{task}",
        subject_kind=TASK_SUBJECT_KIND,
        severity="info",
    )


@pytest_asyncio.fixture(loop_scope="function")
async def capture_on(db_pool):
    """The capture kill switch (`todoist_capture_enabled`) is a settings row
    another file in the same test database may have left off
    (`db/test_migration_011_todoist_capture.py` ends on `false`); the task
    must be created here for the labels to be seen at all. Left at `true`
    afterwards, which is the migration's own default."""
    await db_pool.execute(
        "INSERT INTO settings (key, value) VALUES ('todoist_capture_enabled', 'true'::jsonb) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
    )
    yield


@pytest_asyncio.fixture(loop_scope="function")
async def no_research_agent(db_pool):
    """Every active agent loses the `research` tag for the test, then gets it back."""
    rows = await db_pool.fetch(
        "SELECT id, capabilities FROM agents WHERE active AND capabilities ? 'research'"
    )
    for r in rows:
        await db_pool.execute(
            "UPDATE agents SET capabilities = capabilities - 'research' WHERE id = $1", r["id"]
        )
    try:
        yield [r["id"] for r in rows]
    finally:
        for r in rows:
            await db_pool.execute(
                "UPDATE agents SET capabilities = $2 WHERE id = $1", r["id"], r["capabilities"]
            )


async def test_a_research_task_is_labelled_by_the_tag_holder(db_pool, inbox, todoist, capture_on):  # noqa: F811
    r = await ingest_event(db_pool, _question(), now=NOW)
    out = await project(db_pool, r.problem_id, now=NOW)
    assert out.get("created") is True, out
    args = _cmds(todoist, "item_add")[-1]["args"]
    assert args["labels"] == ["#research", "@raphael", "@next"]


async def test_with_no_holder_the_task_keeps_its_gtd_state_and_no_assignee(
    db_pool, inbox, todoist, capture_on, no_research_agent  # noqa: F811
):
    assert no_research_agent, "the seed's research agent was expected to hold the tag"
    r = await ingest_event(db_pool, _question(), now=NOW)
    out = await project(db_pool, r.problem_id, now=NOW)
    assert out.get("created") is True, out
    args = _cmds(todoist, "item_add")[-1]["args"]
    assert args["labels"] == ["#research", "@next"], "no made-up @label, still a GTD state"
