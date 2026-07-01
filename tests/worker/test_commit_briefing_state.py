"""commit_briefing_state — settings upsert round-trip."""
from __future__ import annotations

import pytest
from aegis.db import run_migrations
from aegis_worker.activities.briefing import BriefingActivities


@pytest.mark.asyncio
async def test_commit_round_trip(db_pool):
    await run_migrations(db_pool)
    act = BriefingActivities(db_pool=db_pool)
    state = {"last_briefing_at": "2026-06-23T00:00:00+00:00",
             "contradiction_count": 5, "seen_intel_ids": ["a", "b"],
             "seen_calendar_ids": ["e1"]}
    await act.commit_briefing_state(state)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM settings WHERE key='briefing_state'")
    assert row["value"]["contradiction_count"] == 5
    assert row["value"]["seen_intel_ids"] == ["a", "b"]
    # upsert overwrites
    await act.commit_briefing_state({**state, "contradiction_count": 9})
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM settings WHERE key='briefing_state'")
    assert row["value"]["contradiction_count"] == 9
