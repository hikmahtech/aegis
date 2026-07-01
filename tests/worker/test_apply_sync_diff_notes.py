"""apply_sync_diff projects Todoist notes and bumps last_note_at — with the
comment-loop guard that skips bumps for AEGIS-authored comments."""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aegis_worker.activities.todoist import TodoistActivities


@pytest_asyncio.fixture(loop_scope="function")
async def _bootstrapped_task(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, raw) "
            "VALUES ('P1', 'X', false, '{}'::jsonb) "
            "ON CONFLICT (id) DO NOTHING"
        )
        await conn.execute(
            "INSERT INTO todoist_tasks (id, project_id, content, labels, raw) "
            "VALUES ('T1', 'P1', 'demo', ARRAY[]::text[], '{}'::jsonb) "
            "ON CONFLICT (id) DO NOTHING"
        )
        # Reset per-test state in case a prior test set last_note_at or
        # inserted notes against this task — both tables persist across
        # tests in the shared dev database.
        await conn.execute(
            "UPDATE todoist_tasks SET last_note_at = NULL WHERE id = 'T1'"
        )
        await conn.execute("DELETE FROM todoist_notes WHERE item_id = 'T1'")
    yield "T1"


@pytest.mark.asyncio
async def test_user_note_projected_and_bumps_last_note_at(
    db_pool, _bootstrapped_task
) -> None:
    acts = TodoistActivities(db_pool=db_pool, connector=AsyncMock())
    posted = dt.datetime(2026, 5, 19, 12, 0, 0, tzinfo=dt.UTC)
    diff = {
        "projects": [],
        "labels": [],
        "items": [],
        "notes": [
            {
                "id": "N1",
                "item_id": "T1",
                "content": "user wrote this",
                "posted_uid": "12345",
                "posted_at": posted.isoformat(),
            }
        ],
    }
    await acts.apply_sync_diff(diff)
    async with db_pool.acquire() as conn:
        note_row = await conn.fetchrow(
            "SELECT content, posted_at FROM todoist_notes WHERE id='N1'"
        )
        task_row = await conn.fetchrow(
            "SELECT last_note_at FROM todoist_tasks WHERE id='T1'"
        )
    assert note_row["content"] == "user wrote this"
    assert note_row["posted_at"] == posted
    assert task_row["last_note_at"] == posted


@pytest.mark.asyncio
async def test_clarify_note_projected_but_does_not_bump_last_note_at(
    db_pool, _bootstrapped_task
) -> None:
    acts = TodoistActivities(db_pool=db_pool, connector=AsyncMock())
    posted = dt.datetime(2026, 5, 19, 13, 0, 0, tzinfo=dt.UTC)
    diff = {
        "projects": [],
        "labels": [],
        "items": [],
        "notes": [
            {
                "id": "N2",
                "item_id": "T1",
                "content": "[ClarifyFlow @ 12:30 UTC · pass 1] @me · trash",
                "posted_uid": None,
                "posted_at": posted.isoformat(),
            }
        ],
    }
    await acts.apply_sync_diff(diff)
    async with db_pool.acquire() as conn:
        note_row = await conn.fetchrow(
            "SELECT content FROM todoist_notes WHERE id='N2'"
        )
        last_note_at = await conn.fetchval(
            "SELECT last_note_at FROM todoist_tasks WHERE id='T1'"
        )
    assert note_row["content"].startswith("[ClarifyFlow @ ")
    # Comment-loop guard: AEGIS-authored note projected but DOES NOT bump
    # last_note_at, otherwise ClarifyFlow would re-process the task forever.
    assert last_note_at is None


@pytest.mark.asyncio
async def test_aegis_workflow_run_note_does_not_bump_last_note_at(
    db_pool, _bootstrapped_task
) -> None:
    """AlertInvestigationFlow's voice-line comments include a stable
    `Workflow run: <id>` footer. The bump guard treats those as
    AEGIS-authored so they don't trigger pandora_followup re-routing
    in ClarifyFlow. Without this, every successful investigation would
    cause ClarifyFlow to spawn another one every 15 min."""
    acts = TodoistActivities(db_pool=db_pool, connector=AsyncMock())
    posted = dt.datetime(2026, 5, 21, 12, 0, 0, tzinfo=dt.UTC)
    diff = {
        "projects": [],
        "labels": [],
        "items": [],
        "notes": [
            {
                "id": "N_AEGIS_WR",
                "item_id": "T1",
                "content": (
                    "🎭 the owner-sama — scoping complete. Findings actionable.\n\n"
                    "Summary: bug reproduces on staging.\n"
                    "Next step: file PR\n"
                    "Workflow run: pandora-jira-T1-scheduled-x"
                ),
                "posted_at": posted.isoformat(),
            }
        ],
    }
    await acts.apply_sync_diff(diff)
    async with db_pool.acquire() as conn:
        note = await conn.fetchval(
            "SELECT content FROM todoist_notes WHERE id='N_AEGIS_WR'"
        )
        last_note_at = await conn.fetchval(
            "SELECT last_note_at FROM todoist_tasks WHERE id='T1'"
        )
    assert note is not None and "Workflow run:" in note  # projected
    assert last_note_at is None  # bump suppressed


@pytest.mark.asyncio
async def test_last_note_at_takes_max_of_multiple_user_notes(
    db_pool, _bootstrapped_task
) -> None:
    acts = TodoistActivities(db_pool=db_pool, connector=AsyncMock())
    earlier = dt.datetime(2026, 5, 19, 10, 0, 0, tzinfo=dt.UTC)
    later = dt.datetime(2026, 5, 19, 14, 0, 0, tzinfo=dt.UTC)
    diff = {
        "projects": [],
        "labels": [],
        "items": [],
        "notes": [
            {"id": "N3", "item_id": "T1", "content": "first", "posted_at": earlier.isoformat()},
            {"id": "N4", "item_id": "T1", "content": "second", "posted_at": later.isoformat()},
        ],
    }
    await acts.apply_sync_diff(diff)
    async with db_pool.acquire() as conn:
        last_note_at = await conn.fetchval(
            "SELECT last_note_at FROM todoist_tasks WHERE id='T1'"
        )
    assert last_note_at == later
