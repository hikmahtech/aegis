"""#508 — tracked topics reach the scans, and a worthy item with no snippet is
read from its page instead of being dropped."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from aegis_worker.activities.intelligence import (
    TRACKED_TOPICS_SETTING,
    IntelligenceActivities,
    tracked_search_terms,
)
from temporalio.testing import ActivityEnvironment

# --------------------------------------------------------------------------
# tracked_search_terms — the pure parse of the `intelligence_topics` row.
# --------------------------------------------------------------------------


def test_tracked_search_terms_uses_queries_then_the_name():
    value = {
        "topics": [
            {"name": "crypto", "queries": ["bitcoin", " ethereum "], "priority": "high"},
            {"name": "rust", "priority": "medium"},
            {"name": "ai", "queries": []},
        ]
    }
    assert tracked_search_terms(value) == ["bitcoin", "ethereum", "rust", "ai"]


def test_tracked_search_terms_reads_a_json_string():
    raw = json.dumps({"topics": [{"name": "x", "queries": ["q"]}]})
    assert tracked_search_terms(raw) == ["q"]


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "not json",
        [],
        {"topics": "x"},
        {"topics": [None, 3, {"queries": "bitcoin"}]},
    ],
)
def test_tracked_search_terms_never_raises_on_a_bad_row(value):
    assert tracked_search_terms(value) == []


# --------------------------------------------------------------------------
# load_tracked_topics — reads the row track_topic writes, on real Postgres.
# --------------------------------------------------------------------------


async def test_load_tracked_topics_reads_what_track_topic_writes(db_pool):
    # The same shape _exec_track_topic writes.
    await db_pool.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()",
        TRACKED_TOPICS_SETTING,
        {
            "topics": [
                {"name": "crypto", "queries": ["bitcoin", "ethereum"], "priority": "high"},
                {"name": "rust", "queries": [], "priority": "medium"},
            ]
        },
    )
    try:
        terms = await ActivityEnvironment().run(
            IntelligenceActivities(db_pool=db_pool).load_tracked_topics
        )
    finally:
        await db_pool.execute("DELETE FROM settings WHERE key = $1", TRACKED_TOPICS_SETTING)
    assert terms == ["bitcoin", "ethereum", "rust"]


async def test_load_tracked_topics_with_no_row_is_no_topics(db_pool):
    await db_pool.execute("DELETE FROM settings WHERE key = $1", TRACKED_TOPICS_SETTING)
    terms = await ActivityEnvironment().run(
        IntelligenceActivities(db_pool=db_pool).load_tracked_topics
    )
    assert terms == []


# --------------------------------------------------------------------------
# ingest_intelligence — an item with no snippet is read from its page.
# --------------------------------------------------------------------------

_FETCH = "aegis_worker.activities.intelligence.fetch_and_extract"


@pytest.fixture
def kc():
    kc = AsyncMock()
    kc.ingest_content = AsyncMock(return_value={"status": "ok"})
    return kc


async def test_an_item_with_no_snippet_is_read_from_its_page(kc):
    page = "A long article body about the story. " * 20
    with patch(_FETCH, AsyncMock(return_value=(page, "Page title"))) as fetch:
        result = await ActivityEnvironment().run(
            IntelligenceActivities(knowledge_connector=kc).ingest_intelligence,
            [{"title": "Story", "url": "https://news.example/story", "snippet": ""}],
        )
    assert result["ingested"] == 1
    assert result["fetched"] == 1
    assert result["skipped_no_text"] == 0
    fetch.assert_awaited_once_with("https://news.example/story", "article")
    kwargs = kc.ingest_content.call_args.kwargs
    assert kwargs["raw_text"] == f"Story\n\n{page}"
    assert kwargs["summary"] == page[:500]
    assert kwargs["source_type"] == "intelligence"
    assert kwargs["url"] == "https://news.example/story"


async def test_a_page_with_too_little_text_is_still_skipped(kc):
    with patch(_FETCH, AsyncMock(return_value=("Subscribe to read.", None))):
        result = await ActivityEnvironment().run(
            IntelligenceActivities(knowledge_connector=kc).ingest_intelligence,
            [{"title": "Paywalled", "url": "https://news.example/paywall", "snippet": ""}],
        )
    assert result["ingested"] == 0
    assert result["fetched"] == 0
    assert result["skipped_no_text"] == 1
    kc.ingest_content.assert_not_called()


async def test_a_page_that_raises_is_skipped_not_fatal(kc):
    with patch(_FETCH, AsyncMock(side_effect=RuntimeError("bad markup"))):
        result = await ActivityEnvironment().run(
            IntelligenceActivities(knowledge_connector=kc).ingest_intelligence,
            [{"title": "Broken", "url": "https://news.example/broken", "snippet": ""}],
        )
    assert result["skipped_no_text"] == 1
    kc.ingest_content.assert_not_called()


async def test_an_item_with_a_snippet_is_not_fetched(kc):
    with patch(_FETCH, AsyncMock(return_value=("unused", None))) as fetch:
        result = await ActivityEnvironment().run(
            IntelligenceActivities(knowledge_connector=kc).ingest_intelligence,
            [{"title": "Story", "url": "https://news.example/s", "snippet": "Short text."}],
        )
    fetch.assert_not_awaited()
    assert result["ingested"] == 1
    assert result["fetched"] == 0
    assert kc.ingest_content.call_args.kwargs["summary"] == "Short text."


async def test_images_are_not_read(kc):
    """This path has no OCR, so an image URL is not worth a fetch."""
    with patch(_FETCH, AsyncMock(return_value=("unused", None))) as fetch:
        result = await ActivityEnvironment().run(
            IntelligenceActivities(knowledge_connector=kc).ingest_intelligence,
            [{"title": "Chart", "url": "https://news.example/chart.png", "snippet": ""}],
        )
    fetch.assert_not_awaited()
    assert result["skipped_no_text"] == 1
