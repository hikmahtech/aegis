"""apply_sync_diff handles the parent-FK edge cases:

  1. Child appearing before parent in the same batch (topological reorder).
  2. parent_id pointing to a task that is neither in the batch nor in our
     projection (orphan — null out the parent_id and proceed).

Caught 2026-05-21 in prod: TodoistSyncFlow was failing every 5 min with
`asyncpg.exceptions.ForeignKeyViolationError: todoist_tasks_parent_id_fkey`
because the previous code relied on Todoist's API returning parents first,
which isn't guaranteed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aegis_worker.activities.todoist import TodoistActivities


@pytest_asyncio.fixture(loop_scope="function")
async def _project(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, raw) "
            "VALUES ('PFK', 'fkproject', false, '{}'::jsonb) "
            "ON CONFLICT (id) DO NOTHING"
        )
        # Clean up any leftover rows from prior runs
        await conn.execute(
            "DELETE FROM todoist_tasks WHERE id IN ('PFK_PARENT', 'PFK_CHILD', 'PFK_ORPHAN')"
        )
    yield "PFK"


@pytest.mark.asyncio
async def test_child_before_parent_in_batch_topo_reorders(db_pool, _project) -> None:
    """Diff with child first, parent second still inserts cleanly thanks
    to the in-activity topological sort."""
    acts = TodoistActivities(db_pool=db_pool, connector=AsyncMock())
    diff = {
        "projects": [],
        "labels": [],
        "items": [
            # Child appears BEFORE parent — the previous code would FK-violate.
            {
                "id": "PFK_CHILD",
                "project_id": "PFK",
                "parent_id": "PFK_PARENT",
                "content": "child",
                "labels": [],
            },
            {
                "id": "PFK_PARENT",
                "project_id": "PFK",
                "parent_id": None,
                "content": "parent",
                "labels": [],
            },
        ],
        "notes": [],
    }
    await acts.apply_sync_diff(diff)
    async with db_pool.acquire() as conn:
        parent = await conn.fetchrow(
            "SELECT id, parent_id FROM todoist_tasks WHERE id = 'PFK_PARENT'"
        )
        child = await conn.fetchrow(
            "SELECT id, parent_id FROM todoist_tasks WHERE id = 'PFK_CHILD'"
        )
    assert parent is not None and parent["parent_id"] is None
    assert child is not None and child["parent_id"] == "PFK_PARENT"


@pytest.mark.asyncio
async def test_orphan_parent_nulled_and_inserted(db_pool, _project) -> None:
    """Item whose parent_id is neither in the batch nor in the projection
    is treated as a top-level task — parent_id nulled, item still
    inserted (rather than dropped, which would hide tasks from the user)."""
    acts = TodoistActivities(db_pool=db_pool, connector=AsyncMock())
    diff = {
        "projects": [],
        "labels": [],
        "items": [
            {
                "id": "PFK_ORPHAN",
                "project_id": "PFK",
                "parent_id": "DOES_NOT_EXIST_ANYWHERE",
                "content": "orphan",
                "labels": [],
            },
        ],
        "notes": [],
    }
    await acts.apply_sync_diff(diff)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, parent_id FROM todoist_tasks WHERE id = 'PFK_ORPHAN'"
        )
    assert row is not None
    assert row["parent_id"] is None
