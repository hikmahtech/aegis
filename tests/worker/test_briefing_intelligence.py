"""Tests for intelligence section in daily briefing."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from aegis_worker.activities.briefing import BriefingActivities
from temporalio.testing import ActivityEnvironment


def _iso(dt: datetime) -> str:
    return dt.replace(tzinfo=UTC).isoformat()


async def test_gather_intelligence_summary_filters_by_significance():
    kc = AsyncMock()
    kc.search = AsyncMock(
        return_value=[
            {
                "content": "GPT-5 released",
                "metadata": {"topic": "ai", "significance": 4},
                "source_type": "intelligence",
            },
            {
                "content": "BRICS expands",
                "metadata": {"topic": "geopolitics", "significance": 4},
                "source_type": "intelligence",
            },
            {
                "content": "Minor news",
                "metadata": {"topic": "finance", "significance": 2},
                "source_type": "intelligence",
            },
        ]
    )
    act = BriefingActivities(db_pool=AsyncMock(), knowledge_connector=kc)
    env = ActivityEnvironment()
    result = await env.run(act.gather_intelligence_summary, 24)
    assert isinstance(result, list)
    assert len(result) == 2  # only significance >= 3


async def test_gather_intelligence_no_connector():
    act = BriefingActivities(db_pool=AsyncMock())
    env = ActivityEnvironment()
    result = await env.run(act.gather_intelligence_summary, 24)
    assert result == []


async def test_gather_intelligence_passes_source_type():
    kc = AsyncMock()
    kc.search = AsyncMock(return_value=[])
    act = BriefingActivities(db_pool=None, knowledge_connector=kc)
    env = ActivityEnvironment()
    await env.run(act.gather_intelligence_summary, 24)
    kc.search.assert_called_once()
    call_kwargs = kc.search.call_args
    assert (
        call_kwargs.kwargs.get("source_type") == "intelligence"
        or call_kwargs.args[2] == "intelligence"
    )


async def test_gather_intelligence_error_returns_empty():
    kc = AsyncMock()
    kc.search = AsyncMock(side_effect=Exception("network error"))
    act = BriefingActivities(db_pool=None, knowledge_connector=kc)
    env = ActivityEnvironment()
    result = await env.run(act.gather_intelligence_summary, 24)
    assert result == []


async def test_gather_intelligence_significance_boundary():
    """Items with significance exactly 3 should be included."""
    kc = AsyncMock()
    kc.search = AsyncMock(
        return_value=[
            {"content": "Exactly at threshold", "metadata": {"significance": 3}},
            {"content": "Below threshold", "metadata": {"significance": 2}},
            {"content": "Above threshold", "metadata": {"significance": 5}},
            {"content": "No significance key", "metadata": {}},
        ]
    )
    act = BriefingActivities(db_pool=None, knowledge_connector=kc)
    env = ActivityEnvironment()
    result = await env.run(act.gather_intelligence_summary, 24)
    # significance >= 3: items at 3 and 5 qualify; missing key defaults to 0
    assert len(result) == 2


async def test_gather_intelligence_empty_results():
    kc = AsyncMock()
    kc.search = AsyncMock(return_value=[])
    act = BriefingActivities(db_pool=None, knowledge_connector=kc)
    env = ActivityEnvironment()
    result = await env.run(act.gather_intelligence_summary, 24)
    assert result == []


async def test_gather_intelligence_summary_honours_hours_window():
    """The `hours` parameter actually filters by ingested_at — earlier
    code silently ignored it. Items outside the window get dropped;
    items missing ingested_at stay (defence)."""
    now = datetime.now(UTC)
    kc = AsyncMock()
    kc.search = AsyncMock(
        return_value=[
            {
                "title": "Inside window",
                "metadata": {"significance": 5},
                "ingested_at": _iso(now - timedelta(hours=2)),
            },
            {
                "title": "Outside window",
                "metadata": {"significance": 5},
                "ingested_at": _iso(now - timedelta(hours=48)),
            },
            {
                "title": "No ts (defence — keep)",
                "metadata": {"significance": 5},
            },
        ]
    )
    act = BriefingActivities(db_pool=None, knowledge_connector=kc)
    env = ActivityEnvironment()
    result = await env.run(act.gather_intelligence_summary, 24)
    titles = {r["title"] for r in result}
    assert "Inside window" in titles
    assert "Outside window" not in titles
    assert "No ts (defence — keep)" in titles
