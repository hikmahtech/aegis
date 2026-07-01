"""gather_today_focus — ranked @me next actions, excluding parked/managed."""
from __future__ import annotations

import pytest
from aegis.db import run_migrations
from aegis_worker.activities.review import ReviewActivities, format_today_focus


@pytest.mark.asyncio
async def test_today_focus_ranks_and_excludes(db_pool):
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_managed_project_ids', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            {"inbox": "P_INB_F", "next": "P_NXT_F", "someday": "P_SOM_F"},
        )
        for pid in ("P_INB_F", "P_NXT_F", "P_SOM_F"):
            await conn.execute(
                "INSERT INTO todoist_projects (id, name, is_managed, raw) "
                "VALUES ($1,$1,true,'{}'::jsonb) ON CONFLICT (id) DO NOTHING", pid,
            )
        await conn.execute("DELETE FROM todoist_tasks WHERE project_id LIKE 'P_%_F'")
        # actionable @me (overdue) — should appear
        await conn.execute(
            "INSERT INTO todoist_tasks (id, project_id, content, labels, assignee_label, due_date, is_completed, raw) "
            "VALUES ('F_DUE','P_NXT_F','pay bill','{@me}','@me',CURRENT_DATE-1,false,'{}'::jsonb)"
        )
        # @waiting — excluded
        await conn.execute(
            "INSERT INTO todoist_tasks (id, project_id, content, labels, assignee_label, is_completed, raw) "
            "VALUES ('F_WAIT','P_NXT_F','blocked','{@me,@waiting}','@me',false,'{}'::jsonb)"
        )
        # someday project — excluded
        await conn.execute(
            "INSERT INTO todoist_tasks (id, project_id, content, labels, assignee_label, is_completed, raw) "
            "VALUES ('F_SOM','P_SOM_F','later','{@me}','@me',false,'{}'::jsonb)"
        )
    items = await ReviewActivities(db_pool=db_pool).gather_today_focus()
    ids = [i["task_id"] for i in items]
    assert "F_DUE" in ids
    assert "F_WAIT" not in ids
    assert "F_SOM" not in ids
    body = format_today_focus(items)
    assert "pay bill" in body


def test_format_today_focus_empty():
    body = format_today_focus([])
    assert "clear" in body.lower() or "nothing" in body.lower()
