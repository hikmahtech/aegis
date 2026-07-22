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
    """Stalled-project detection now requires a LEAF work-stream project —
    a real Todoist project with parent_id IS NOT NULL, nested under an AREA
    project (Todoist restructure, 2026-07: project/* labels are retired).
    _seeded plants P_STALL_D / P_OK_D as top-level projects (parent_id
    NULL), so nest them under a freshly-inserted AREA project here before
    running the detectors — otherwise neither is eligible for the
    'stalled' check regardless of task state.
    """
    area_id = "P_AREA_D"
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, parent_id, is_managed, is_archived, raw) "
            "VALUES ($1, 'Area D', NULL, false, false, '{}'::jsonb) "
            "ON CONFLICT (id) DO NOTHING",
            area_id,
        )
        await conn.execute(
            "UPDATE todoist_projects SET parent_id=$1 WHERE id IN ('P_STALL_D','P_OK_D')",
            area_id,
        )
    try:
        snap = await ReviewActivities(db_pool=db_pool).gather_weekly_state()
        assert any(p["project_id"] == "P_STALL_D" for p in snap["stalled_projects"])
        assert not any(p["project_id"] == "P_OK_D" for p in snap["stalled_projects"])
        assert any(i["task_id"] == "T_W8" and i["days"] >= 7 for i in snap["aging_waiting_items"])
        assert any(i["task_id"] == "T_OVD" for i in snap["slipping_items"])
        assert snap["to_read_count"] >= 2
        assert any(i["task_id"] == "T_SM1" for i in snap["someday_resurface_items"])
        assert snap["_top_n"] == 5
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE todoist_projects SET parent_id=NULL "
                "WHERE id IN ('P_STALL_D','P_OK_D')"
            )
            await conn.execute("DELETE FROM todoist_projects WHERE id=$1", area_id)


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
    assert snap["claimed_stale_items"] == []


@pytest.mark.asyncio
async def test_claimed_stale_detector_weekly(db_pool):
    """Issue #117: gather_weekly_state surfaces Inbox tasks claimed with @me,
    watermark >Nd old, no note activity since, as claimed_stale_items."""
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_managed_project_ids', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            {"inbox": "P_INB_W"},
        )
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, raw) "
            "VALUES ('P_INB_W','P_INB_W',true,'{}'::jsonb) ON CONFLICT (id) DO NOTHING"
        )
        await conn.execute("DELETE FROM todoist_tasks WHERE project_id='P_INB_W'")
        await conn.execute(
            "INSERT INTO todoist_tasks (id, project_id, content, labels, "
            "last_clarified_at, last_note_at, is_completed, raw) VALUES "
            "('T_WCS','P_INB_W','APP-11235: claimed then abandoned',ARRAY['@me'],"
            "now()-interval '7 days', now()-interval '7 days', false, '{}'::jsonb), "
            "('T_WACT','P_INB_W','APP-11236: active',ARRAY['@me'],"
            "now()-interval '7 days', now()-interval '1 day', false, '{}'::jsonb)"
        )
    snap = await ReviewActivities(db_pool=db_pool).gather_weekly_state()
    ids = {i["task_id"] for i in snap["claimed_stale_items"]}
    assert "T_WCS" in ids
    assert "T_WACT" not in ids
    item = next(i for i in snap["claimed_stale_items"] if i["task_id"] == "T_WCS")
    assert item["days"] >= 5


@pytest.mark.asyncio
async def test_aging_waiting_streak_from_prior_digests(db_pool):
    """Issue #117: an aging_waiting item that appeared in the two most-recent
    prior weekly digests accumulates streak == 3 (current + 2 prior)."""
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_managed_project_ids', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            {"inbox": "P_INB_STK"},
        )
        await conn.execute(
            "INSERT INTO todoist_projects (id, name, is_managed, raw) "
            "VALUES ('P_INB_STK','P_INB_STK',true,'{}'::jsonb) ON CONFLICT (id) DO NOTHING"
        )
        await conn.execute("DELETE FROM todoist_tasks WHERE id='T_STK'")
        await conn.execute(
            "INSERT INTO todoist_tasks (id, project_id, content, labels, "
            "is_completed, updated_at, raw) VALUES "
            "('T_STK','P_INB_STK','chase the vendor',ARRAY['@waiting'],false,"
            "now()-interval '17 days','{}'::jsonb)"
        )
        # Two prior WEEKLY digests that both listed T_STK. Inserted last, so
        # they are the most-recent rows in the streak lookback window.
        for _ in range(2):
            await conn.execute(
                "INSERT INTO review_digest_log (review_kind, counts, preview) "
                "VALUES ('weekly', $1, 'p')",
                {"aging_waiting_items": [
                    {"task_id": "T_STK", "content": "chase the vendor", "days": 10}
                ]},
            )
    snap = await ReviewActivities(db_pool=db_pool).gather_weekly_state()
    item = next((i for i in snap["aging_waiting_items"] if i["task_id"] == "T_STK"), None)
    assert item is not None
    assert item["streak"] == 3


def test_build_decisions_escalates_after_three_keeps():
    """Issue #117: a streak>=3 aging_waiting item escalates — the card leads
    with drop/done and notes 'kept Nw running'. A first appearance is unchanged."""
    acts = ReviewActivities(db_pool=None)
    snap = {
        "aging_waiting_items": [
            {"task_id": "T_HOT", "content": "chase vendor", "days": 17, "streak": 3},
            {"task_id": "T_NEW", "content": "new wait", "days": 8, "streak": 1},
        ],
    }
    decs = acts._build_decisions(snap)
    hot = next(d for d in decs if d["task_id"] == "T_HOT")
    new = next(d for d in decs if d["task_id"] == "T_NEW")
    # Escalated: leads with 'drop', mentions the running streak.
    assert list(hot["options"].keys())[0] == "drop"
    assert "kept 2w running" in hot["prompt"]
    # First appearance: unchanged — leads with 'nudge', no streak note.
    assert list(new["options"].keys())[0] == "nudge"
    assert "running" not in new["prompt"]


def test_build_decisions_emits_claimed_stale_cards():
    """Issue #117: claimed_stale_items become decision cards (reused card
    machinery, no new interaction kind)."""
    acts = ReviewActivities(db_pool=None)
    snap = {"claimed_stale_items": [
        {"task_id": "T_CLM", "content": "APP-11230: investigate", "days": 6},
    ]}
    decs = acts._build_decisions(snap)
    card = next(d for d in decs if d["signal"] == "claimed_stale")
    assert card["task_id"] == "T_CLM"
    assert set(card["options"].keys()) == {"done", "unclaim", "keep"}
    assert "Claimed" in card["prompt"]
