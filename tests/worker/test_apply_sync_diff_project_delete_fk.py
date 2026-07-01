"""apply_sync_diff defers project DELETEs until after task DELETEs.

Caught 2026-05-25 in prod: TodoistSyncFlow failed every 5min for ~6 hours
with `asyncpg.exceptions.ForeignKeyViolationError: todoist_tasks_project_id_fkey`
because the previous code processed project deletions BEFORE task deletions
within the same transaction. A user-deleted Todoist project whose tasks
were also in the same diff (typical pattern) triggered the FK violation
and rolled back the whole transaction, jamming the sync token.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aegis_worker.activities.todoist import TodoistActivities


@pytest_asyncio.fixture(loop_scope="function")
async def _seed(db_pool):
    """Seed: one project + one task referencing it. The diff in the test
    then asks for both to be deleted, mirroring what Todoist's sync API
    sends when the user deletes a non-empty project."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, raw) "
            "VALUES ('DEL_PROJ', 'will-be-deleted', false, '{}'::jsonb) "
            "ON CONFLICT (id) DO NOTHING"
        )
        await conn.execute(
            """
            INSERT INTO todoist_tasks (id, project_id, content, labels, raw)
            VALUES ('DEL_TASK', 'DEL_PROJ', 'will-also-be-deleted',
                    '{}'::text[], '{}'::jsonb)
            ON CONFLICT (id) DO NOTHING
            """
        )
    yield ("DEL_PROJ", "DEL_TASK")
    async with db_pool.acquire() as conn:
        # Cleanup in FK order — tasks first.
        await conn.execute("DELETE FROM todoist_tasks WHERE id = 'DEL_TASK'")
        await conn.execute("DELETE FROM todoist_projects WHERE id = 'DEL_PROJ'")


@pytest.mark.asyncio
async def test_project_delete_with_referencing_task_in_same_diff(db_pool, _seed) -> None:
    """Diff carrying `project.is_deleted=true` AND `task.is_deleted=true`
    for a task that references the project must apply cleanly. Pre-fix
    this raised ForeignKeyViolationError and rolled back the txn,
    leaving both rows in place AND the sync_token stuck."""
    proj_id, task_id = _seed
    acts = TodoistActivities(db_pool=db_pool, connector=AsyncMock())
    diff = {
        "projects": [{"id": proj_id, "is_deleted": True}],
        "labels": [],
        "items": [{"id": task_id, "is_deleted": True}],
        "notes": [],
    }
    # Must not raise. Pre-fix: asyncpg.exceptions.ForeignKeyViolationError.
    await acts.apply_sync_diff(diff)
    async with db_pool.acquire() as conn:
        project = await conn.fetchrow(
            "SELECT id FROM todoist_projects WHERE id = $1", proj_id
        )
        task = await conn.fetchrow("SELECT id FROM todoist_tasks WHERE id = $1", task_id)
    assert project is None, "project should have been DELETE'd"
    assert task is None, "task should have been DELETE'd"
