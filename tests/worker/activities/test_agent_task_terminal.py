"""Terminal-state writers. Every non-complete exit MUST park at @waiting —
otherwise the 6h cooldown becomes an infinite slow loop over the same tasks."""

from __future__ import annotations

import pytest_asyncio
from aegis_worker.activities.agent_task import PARK_LABEL, AgentTaskActivities


@pytest_asyncio.fixture(loop_scope="function")
async def _seed(db_pool):
    await db_pool.execute("DELETE FROM todoist_tasks WHERE id LIKE 'tm-%'")
    await db_pool.execute("DELETE FROM todoist_outbox WHERE temp_id LIKE 'agent-task-%'")
    await db_pool.execute(
        "INSERT INTO todoist_tasks (id, content, labels, source_tag, assignee_label, is_completed) "
        "VALUES ('tm-1','a task', ARRAY['@pandora'], '#alert', '@pandora', false)"
    )
    yield
    await db_pool.execute("DELETE FROM todoist_tasks WHERE id LIKE 'tm-%'")
    await db_pool.execute("DELETE FROM todoist_outbox WHERE temp_id LIKE 'agent-task-%'")


async def test_park_task_adds_waiting_label_locally_and_to_outbox(db_pool, _seed):
    act = AgentTaskActivities(db_pool=db_pool)
    assert (await act.park_task("tm-1", "needs a human"))["parked"] is True

    labels = await db_pool.fetchval("SELECT labels FROM todoist_tasks WHERE id = 'tm-1'")
    assert PARK_LABEL in labels
    queued = await db_pool.fetchval(
        "SELECT count(*) FROM todoist_outbox WHERE temp_id = 'agent-task-park-tm-1'"
    )
    assert queued == 1


async def test_park_task_is_idempotent(db_pool, _seed):
    act = AgentTaskActivities(db_pool=db_pool)
    await act.park_task("tm-1", "first")
    await act.park_task("tm-1", "second")
    labels = await db_pool.fetchval("SELECT labels FROM todoist_tasks WHERE id = 'tm-1'")
    assert labels.count(PARK_LABEL) == 1


async def test_complete_task_queues_item_complete(db_pool, _seed):
    act = AgentTaskActivities(db_pool=db_pool)
    assert (await act.complete_task("tm-1"))["completed"] is True
    cmd = await db_pool.fetchval(
        "SELECT command FROM todoist_outbox WHERE temp_id = 'agent-task-complete-tm-1'"
    )
    assert cmd["type"] == "item_complete"


async def test_park_missing_task_is_false_not_crash(db_pool, _seed):
    assert (await AgentTaskActivities(db_pool=db_pool).park_task("tm-absent", "x"))["parked"] is False


async def test_comment_body_carries_the_workflow_run_footer(db_pool, _seed):
    """Without this marker clarify treats the comment as fresh user input and
    re-spawns the flow every 15 minutes (loop shipped twice: 2026-05-21, 05-27).

    The fake connector exposes `commands()` (the real TodoistConnector
    interface — see build_note_add_command / check_sync_status usage
    throughout activities/alerts.py::post_task_note), not a nonexistent
    `add_note()`.
    """
    sent: dict = {}

    class _Todoist:
        @staticmethod
        async def commands(cmds: list[dict]) -> dict:
            sent["cmds"] = cmds
            return {"ok": True}

    act = AgentTaskActivities(db_pool=db_pool, todoist_connector=_Todoist())
    assert (await act.comment("tm-1", "pandoras-actor", "found the cause"))["ok"] is True
    # Prove the fake was actually exercised — a connector that silently
    # no-oped would otherwise still pass every assertion below.
    assert len(sent["cmds"]) == 1
    assert sent["cmds"][0]["type"] == "note_add"
    content = sent["cmds"][0]["args"]["content"]
    assert sent["cmds"][0]["args"]["item_id"] == "tm-1"
    assert "Workflow run:" in content
    assert "found the cause" in content
