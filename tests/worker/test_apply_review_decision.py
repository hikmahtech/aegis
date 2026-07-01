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
async def test_someday_activate_moves_to_next(db_pool):
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('todoist_managed_project_ids', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            {"inbox": "P_I", "next": "P_NEXT_ACT", "someday": "P_S"},
        )
    fake = _FakeTodoist()
    acts = ReviewActivities(db_pool=db_pool, todoist_connector=fake)
    out = await acts.apply_review_decision(
        "i1", {"value": "activate"},
        {"signal": "someday_resurface", "task_id": "T_SM"})
    assert out["applied"] is True
    cmd = fake.batches[0][0]
    assert cmd["type"] == "item_move"
    assert cmd["args"]["project_id"] == "P_NEXT_ACT"
