"""IntelScanActivities — searxng queries."""

from __future__ import annotations

import pytest
import respx
from aegis_worker.activities.intel_scan import (
    IntelScanActivities,
    SearchSourceInput,
    SearchSourceResult,
)
from httpx import Response
from temporalio.testing import ActivityEnvironment


@pytest.fixture
def intel():
    return IntelScanActivities(searxng_url="http://searxng:8080")


@pytest.mark.asyncio
@respx.mock
async def test_search_hn_single_topic(intel):
    respx.get("http://searxng:8080/search").mock(
        return_value=Response(
            200,
            json={
                "results": [
                    {
                        "title": "HN item",
                        "url": "https://news.ycombinator.com/item?id=1",
                        "content": "a hackery thing",
                        "publishedDate": "2026-04-18",
                    },
                ]
            },
        )
    )
    env = ActivityEnvironment()
    result = await env.run(
        intel.search_source,
        SearchSourceInput(source="hn", topics=["rust"]),
    )
    assert isinstance(result, SearchSourceResult)
    assert result.source == "hn"
    assert len(result.items) == 1
    assert result.items[0]["title"] == "HN item"
    assert result.items[0]["source"] == "hn"


@pytest.mark.asyncio
@respx.mock
async def test_search_news_multi_topic_deduped(intel):
    # Same URL returned for both topics → dedup on URL
    respx.get("http://searxng:8080/search").mock(
        side_effect=[
            Response(
                200,
                json={
                    "results": [
                        {"title": "T1", "url": "https://x.com/a", "content": "c1"},
                        {"title": "T2", "url": "https://x.com/b", "content": "c2"},
                    ]
                },
            ),
            Response(
                200,
                json={
                    "results": [
                        {"title": "T1-dup", "url": "https://x.com/a", "content": "c1"},  # dup
                        {"title": "T3", "url": "https://x.com/c", "content": "c3"},
                    ]
                },
            ),
        ]
    )
    env = ActivityEnvironment()
    result = await env.run(
        intel.search_source,
        SearchSourceInput(source="news", topics=["ai", "systems"]),
    )
    assert len(result.items) == 3  # deduped to 3 unique URLs
    urls = {it["url"] for it in result.items}
    assert urls == {"https://x.com/a", "https://x.com/b", "https://x.com/c"}


@pytest.mark.asyncio
@respx.mock
async def test_one_failing_topic_degrades_that_topic_only(intel):
    """issue #136: a single bad topic query must not sink the whole source.

    Before the per-topic guard, `resp.raise_for_status()` on topic 1 aborted
    the activity, ACT_RETRY replayed it 3x, and the workflow hard-failed —
    discarding topic 2's perfectly good results. The load-bearing assertion is
    that the OTHER topic's results survived, not merely that nothing raised.
    """
    respx.get("http://searxng:8080/search").mock(
        side_effect=[
            Response(502, text="searxng upstream boom"),
            Response(
                200,
                json={
                    "results": [
                        {"title": "Good1", "url": "https://x.com/g1", "content": "c1"},
                        {"title": "Good2", "url": "https://x.com/g2", "content": "c2"},
                    ]
                },
            ),
        ]
    )
    env = ActivityEnvironment()
    result = await env.run(
        intel.search_source,
        SearchSourceInput(source="news", topics=["bad", "good"]),
    )
    assert result.failed_topics == ["bad"]
    # The surviving topic's items are all there and intact.
    assert {it["url"] for it in result.items} == {
        "https://x.com/g1",
        "https://x.com/g2",
    }
    assert {it["title"] for it in result.items} == {"Good1", "Good2"}


@pytest.mark.asyncio
@respx.mock
async def test_all_topics_failing_still_raises(intel):
    """The per-topic guard degrades a PARTIAL outage; it must not hide a total
    one. When every query fails, searxng itself is down and the run has to be
    recorded as failed rather than completing green with zero results."""
    respx.get("http://searxng:8080/search").mock(return_value=Response(503, text="down"))
    env = ActivityEnvironment()
    with pytest.raises(RuntimeError, match="all 2 topic queries failed"):
        await env.run(
            intel.search_source,
            SearchSourceInput(source="news", topics=["a", "b"]),
        )


@pytest.mark.asyncio
async def test_empty_searxng_url_returns_empty():
    i = IntelScanActivities(searxng_url="")
    env = ActivityEnvironment()
    result = await env.run(
        i.search_source,
        SearchSourceInput(source="hn", topics=["rust"]),
    )
    assert result.items == []


@pytest.mark.asyncio
@respx.mock
async def test_max_results_trims(intel):
    # 5 results back, max=2
    respx.get("http://searxng:8080/search").mock(
        return_value=Response(
            200,
            json={
                "results": [
                    {"title": f"T{i}", "url": f"https://x.com/{i}", "content": ""} for i in range(5)
                ]
            },
        )
    )
    env = ActivityEnvironment()
    result = await env.run(
        intel.search_source,
        SearchSourceInput(source="hn", topics=["rust"], max_results=2),
    )
    assert len(result.items) == 2
