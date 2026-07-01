"""Tests for BriefingActivities calendar integration."""

from unittest.mock import AsyncMock

from aegis_worker.activities.briefing import BriefingActivities
from temporalio.testing import ActivityEnvironment


async def test_gather_calendar_events_with_data():
    """Calendar events read from settings KV."""
    pool = AsyncMock()
    pool.fetch.return_value = [
        {
            "key": "calendar_events_personal",
            "value": [
                {"id": "evt-1", "summary": "Standup", "start": "2026-03-19T09:00:00Z"},
                {"id": "evt-2", "summary": "Lunch", "start": "2026-03-19T12:00:00Z"},
            ],
        },
    ]
    activities = BriefingActivities(db_pool=pool)
    env = ActivityEnvironment()
    result = await env.run(activities.gather_calendar_events)
    assert result["count"] == 2
    assert result["events"][0]["summary"] == "Standup"


async def test_gather_calendar_events_no_data():
    """No calendar data returns empty."""
    pool = AsyncMock()
    pool.fetch.return_value = []
    activities = BriefingActivities(db_pool=pool)
    env = ActivityEnvironment()
    result = await env.run(activities.gather_calendar_events)
    assert result["count"] == 0
    assert result["events"] == []


async def test_gather_calendar_events_no_pool():
    """No DB pool returns empty."""
    activities = BriefingActivities()
    env = ActivityEnvironment()
    result = await env.run(activities.gather_calendar_events)
    assert result["count"] == 0
