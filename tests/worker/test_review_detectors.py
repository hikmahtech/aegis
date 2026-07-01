"""gather_weekly_state — the four review-copilot signal detectors."""
from __future__ import annotations

import pytest
import pytest_asyncio
from aegis.db import run_migrations
from aegis_worker.activities.review import ReviewActivities


@pytest_asyncio.fixture(loop_scope="function")
async def _seeded(db_pool):
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_managed_project_ids', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            {"inbox": "P_INB_D", "next": "P_NXT_D", "someday": "P_SOM_D"},
        )
        await conn.execute("DELETE FROM todoist_tasks WHERE project_id LIKE 'P_%_D'")
        await conn.execute("DELETE FROM todoist_projects WHERE id LIKE 'P_%_D'")
        # managed projects
        for pid in ("P_INB_D", "P_NXT_D", "P_SOM_D"):
            await conn.execute(
                "INSERT INTO todoist_projects (id, name, is_managed, raw) "
                "VALUES ($1,$1,true,'{}'::jsonb) ON CONFLICT (id) DO NOTHING", pid,
            )
        # STALLED project: open tasks exist but all are @waiting (no actionable)
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, is_archived, raw) "
            "VALUES ('P_STALL_D','Stalled Proj',false,false,'{}'::jsonb)"
        )
        await conn.execute(
            "INSERT INTO todoist_tasks (id, project_id, content, labels, is_completed, raw) "
            "VALUES ('T_ST1','P_STALL_D','blocked thing',ARRAY['@waiting'],false,'{}'::jsonb)"
        )
        # HEALTHY project: has an actionable (no state label) task -> NOT stalled
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, is_archived, raw) "
            "VALUES ('P_OK_D','Healthy Proj',false,false,'{}'::jsonb)"
        )
        await conn.execute(
            "INSERT INTO todoist_tasks (id, project_id, content, labels, assignee_label, is_completed, raw) "
            "VALUES ('T_OK1','P_OK_D','do the thing','{@sebas}','@sebas',false,'{}'::jsonb)"
        )
        # AGING @waiting (8d) with id+url
        await conn.execute(
            "INSERT INTO todoist_tasks (id, project_id, content, labels, is_completed, updated_at, raw) "
            "VALUES ('T_W8','P_NXT_D','chase invoice',ARRAY['@waiting'],false,"
            "now()-interval '8 days','{\"url\":\"https://app.todoist.com/app/task/T_W8\"}'::jsonb)"
        )
        # SLIPPING: overdue task
        await conn.execute(
            "INSERT INTO todoist_tasks (id, project_id, content, labels, due_date, is_completed, raw) "
            "VALUES ('T_OVD','P_NXT_D','file taxes','{@me}',CURRENT_DATE - 3,false,'{}'::jsonb)"
        )
        # TO-READ backlog (2)
        await conn.execute(
            "INSERT INTO todoist_tasks (id, project_id, content, labels, is_completed, raw) VALUES "
            "('T_TR1','P_NXT_D','read A',ARRAY['@to-read'],false,'{}'::jsonb), "
            "('T_TR2','P_NXT_D','read B',ARRAY['@to-read'],false,'{}'::jsonb)"
        )
        # SOMEDAY resurface: old + untouched > 90d. Carries the @someday
        # label (Todoist restructure, 2026-07: Someday/Later is a label
        # now, not a managed project).
        await conn.execute(
            "INSERT INTO todoist_tasks (id, project_id, content, labels, is_completed, updated_at, raw) "
            "VALUES ('T_SM1','P_SOM_D','learn violin',ARRAY['@someday'],false,now()-interval '120 days',"
            "'{\"added_at\":\"2026-01-01T00:00:00Z\"}'::jsonb)"
        )


@pytest.mark.asyncio
async def test_detectors_fire_on_planted(db_pool, _seeded):
    snap = await ReviewActivities(db_pool=db_pool).gather_weekly_state()
    assert any(p["project_id"] == "P_STALL_D" for p in snap["stalled_projects"])
    assert not any(p["project_id"] == "P_OK_D" for p in snap["stalled_projects"])
    assert any(i["task_id"] == "T_W8" and i["days"] >= 7 for i in snap["aging_waiting_items"])
    assert any(i["task_id"] == "T_OVD" for i in snap["slipping_items"])
    assert snap["to_read_count"] >= 2
    assert any(i["task_id"] == "T_SM1" for i in snap["someday_resurface_items"])
    assert snap["_top_n"] == 5


@pytest.mark.asyncio
async def test_detectors_quiet_on_clean(db_pool):
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        # Clean up any previous test data from other tests
        # Must delete dependent tables first. Simplest: only keep non-test projects
        task_ids_to_delete = await conn.fetch(
            "SELECT id FROM todoist_tasks WHERE project_id LIKE 'P_%'"
        )
        for row in task_ids_to_delete:
            await conn.execute("DELETE FROM todoist_notes WHERE item_id = $1", row["id"])
        await conn.execute("DELETE FROM todoist_tasks WHERE project_id LIKE 'P_%'")
        await conn.execute("DELETE FROM todoist_projects WHERE id LIKE 'P_%'")
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_managed_project_ids', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            {"inbox": "P_INB_C", "next": "P_NXT_C", "someday": "P_SOM_C"},
        )
    snap = await ReviewActivities(db_pool=db_pool).gather_weekly_state()
    assert snap["stalled_projects"] == []
    assert snap["aging_waiting_items"] == []
    assert snap["someday_resurface_items"] == []
