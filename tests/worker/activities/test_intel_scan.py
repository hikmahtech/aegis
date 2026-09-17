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


# --------------------------------------------------------------------------
# #585 — every topic gets a share of the results that are scored.
# --------------------------------------------------------------------------


def _per_query(counts: dict[str, int]):
    """A searxng stub: `counts[q]` distinct results for query `q`."""

    def respond(request):
        q = request.url.params["q"]
        return Response(
            200,
            json={
                "results": [
                    {"title": f"{q} {i}", "url": f"https://x.com/{q}/{i}", "content": "c"}
                    for i in range(counts[q])
                ]
            },
        )

    return respond


@pytest.mark.asyncio
@respx.mock
async def test_a_tracked_topic_reaches_the_trimmed_results(intel):
    """The configured topics return 25 results each, more than the 20 slots.
    The old scan kept the first 20 in collection order — all from `ai` — so
    the tracked topic behind them was never scored. Taking turns lets it in."""
    counts = {"ai": 25, "systems": 25, "startups": 25, "rust lang": 2}
    respx.get("http://searxng:8080/search").mock(side_effect=_per_query(counts))
    result = await ActivityEnvironment().run(
        intel.search_source,
        SearchSourceInput(source="news", topics=list(counts), max_results=20, rotation=0),
    )
    urls = [it["url"] for it in result.items]
    assert len(urls) == 20
    assert "https://x.com/rust lang/0" in urls
    assert "https://x.com/rust lang/1" in urls
    # Every configured topic got a share too.
    for q in ("ai", "systems", "startups"):
        assert any(u.startswith(f"https://x.com/{q}/") for u in urls)


@pytest.mark.asyncio
@respx.mock
async def test_one_query_per_topic_and_the_start_rotates(intel):
    """23 topics and 20 slots: one query each, one result each for the 20
    topics the round-robin reaches, and a different start reaches others."""
    topics = [f"topic {i}" for i in range(23)]
    route = respx.get("http://searxng:8080/search").mock(
        side_effect=_per_query(dict.fromkeys(topics, 5))
    )
    env = ActivityEnvironment()
    first = await env.run(
        intel.search_source, SearchSourceInput(source="news", topics=topics, rotation=0)
    )
    assert route.call_count == 23
    assert [it["title"] for it in first.items] == [f"topic {i} 0" for i in range(20)]

    later = await env.run(
        intel.search_source, SearchSourceInput(source="news", topics=topics, rotation=3)
    )
    assert [it["title"] for it in later.items] == [f"topic {i} 0" for i in range(3, 23)]


def test_interleave_skips_a_url_another_topic_took():
    from aegis_worker.activities.intel_scan import interleave

    a = [{"url": "u1"}, {"url": "u2"}]
    b = [{"url": "u1"}, {"url": "u3"}]  # u1 is a's already
    c = []
    out = interleave([a, b, c], limit=10)
    assert [it["url"] for it in out] == ["u1", "u3", "u2"]
    assert interleave([a, b], limit=2, start=1) == [{"url": "u1"}, {"url": "u2"}]
    assert interleave([], limit=5) == []
    assert interleave([a], limit=0) == []
