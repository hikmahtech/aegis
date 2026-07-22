"""apply_review_decision — per-signal Todoist command dispatch."""
from __future__ import annotations

import pytest
from aegis.db import run_migrations
from aegis_worker.activities.review import ReviewActivities


class _FakeTodoist:
    def __init__(self):
        self.batches = []

    async def commands(self, cmds):
        self.batches.append(cmds)
        return {"ok": True, "data": {"sync_status": {c["uuid"]: "ok" for c in cmds}}}


@pytest.mark.asyncio
async def test_keep_is_noop():
    acts = ReviewActivities(db_pool=None, todoist_connector=_FakeTodoist())
    out = await acts.apply_review_decision(
        "i1", {"value": "keep"}, {"signal": "aging_waiting", "task_id": "T1"})
    assert out["applied"] is True and out["noop"] is True
    assert acts.todoist_connector.batches == []


@pytest.mark.asyncio
async def test_waiting_done_completes_task():
    fake = _FakeTodoist()
    acts = ReviewActivities(db_pool=None, todoist_connector=fake)
    out = await acts.apply_review_decision(
        "i1", {"value": "done"}, {"signal": "aging_waiting", "task_id": "T1"})
    assert out["applied"] is True
    cmd = fake.batches[0][0]
    assert cmd["type"] == "item_complete" and cmd["args"]["id"] == "T1"


@pytest.mark.asyncio
async def test_waiting_nudge_posts_note():
    fake = _FakeTodoist()
    acts = ReviewActivities(db_pool=None, todoist_connector=fake)
    await acts.apply_review_decision(
        "i1", {"value": "nudge"}, {"signal": "aging_waiting", "task_id": "T1"})
    cmd = fake.batches[0][0]
    assert cmd["type"] == "note_add" and cmd["args"]["item_id"] == "T1"


@pytest.mark.asyncio
async def test_slipping_tomorrow_updates_due():
    fake = _FakeTodoist()
    acts = ReviewActivities(db_pool=None, todoist_connector=fake)
    await acts.apply_review_decision(
        "i1", {"value": "tomorrow"}, {"signal": "slipping", "task_id": "T9"})
    cmd = fake.batches[0][0]
    assert cmd["type"] == "item_update"
    assert cmd["args"]["id"] == "T9"
    assert cmd["args"]["due"]["string"] == "tomorrow"


@pytest.mark.asyncio
async def test_claimed_stale_done_completes_task():
    """Issue #117: 'done' on a claimed-stale card completes the task."""
    fake = _FakeTodoist()
    acts = ReviewActivities(db_pool=None, todoist_connector=fake)
    out = await acts.apply_review_decision(
        "i1", {"value": "done"}, {"signal": "claimed_stale", "task_id": "T_CS"})
    assert out["applied"] is True
    cmd = fake.batches[0][0]
    assert cmd["type"] == "item_complete" and cmd["args"]["id"] == "T_CS"


@pytest.mark.asyncio
async def test_claimed_stale_unclaim_strips_me_label(db_pool):
    """Issue #117: 'unclaim' removes @me so the task re-enters clarify /
    becomes visible to the other GTD surfaces again."""
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM todoist_tasks WHERE id='T_CS2'")
        await conn.execute(
            "INSERT INTO todoist_tasks (id, content, labels, is_completed, raw) "
            "VALUES ('T_CS2', 'APP-11230: investigate', ARRAY['@me','@deep'], false, '{}'::jsonb)"
        )
    fake = _FakeTodoist()
    acts = ReviewActivities(db_pool=db_pool, todoist_connector=fake)
    out = await acts.apply_review_decision(
        "i1", {"value": "unclaim"},
        {"signal": "claimed_stale", "task_id": "T_CS2"})
    assert out["applied"] is True
    cmd = fake.batches[0][0]
    assert cmd["type"] == "item_update"
    assert cmd["args"]["id"] == "T_CS2"
    assert "@me" not in cmd["args"]["labels"]
    assert "@deep" in cmd["args"]["labels"]


@pytest.mark.asyncio
async def test_claimed_stale_keep_is_noop():
    """Issue #117: 'keep' (Still on it) is a no-op — the card simply
    re-nudges next week."""
    fake = _FakeTodoist()
    acts = ReviewActivities(db_pool=None, todoist_connector=fake)
    out = await acts.apply_review_decision(
        "i1", {"value": "keep"}, {"signal": "claimed_stale", "task_id": "T_CS"})
    assert out["applied"] is True and out["noop"] is True
    assert fake.batches == []


@pytest.mark.asyncio
async def test_someday_activate_swaps_someday_label_for_next(db_pool):
    """'activate' on a someday_resurface item swaps @someday -> @next on
    the task's label set via item_update — Next/Someday are labels now,
    not managed projects (Todoist restructure, 2026-07), so no item_move."""
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM todoist_tasks WHERE id='T_SM'")
        await conn.execute(
            "INSERT INTO todoist_tasks (id, content, labels, is_completed, raw) "
            "VALUES ('T_SM', 'learn violin', ARRAY['@someday','@me'], false, '{}'::jsonb)"
        )
    fake = _FakeTodoist()
    acts = ReviewActivities(db_pool=db_pool, todoist_connector=fake)
    out = await acts.apply_review_decision(
        "i1", {"value": "activate"},
        {"signal": "someday_resurface", "task_id": "T_SM"})
    assert out["applied"] is True
    cmd = fake.batches[0][0]
    assert cmd["type"] == "item_update"
    assert cmd["args"]["id"] == "T_SM"
    assert "@next" in cmd["args"]["labels"]
    assert "@someday" not in cmd["args"]["labels"]
    assert "@me" in cmd["args"]["labels"]
