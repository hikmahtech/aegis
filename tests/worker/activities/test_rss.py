"""RssActivities tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from aegis_worker.activities.rss import (
    FetchFeedInput,
    FetchFeedResult,
    RssActivities,
)
from temporalio.testing import ActivityEnvironment


@pytest.fixture
def rss():
    return RssActivities(db_pool=None)


@pytest.mark.asyncio
async def test_fetch_feed_parses_entries(rss):
    fake_parsed = MagicMock()
    fake_parsed.entries = [
        MagicMock(
            id="id-1",
            title="Post 1",
            link="https://x.com/1",
            summary="s1",
            published_parsed=(2026, 4, 18, 10, 0, 0, 0, 0, 0),
            updated_parsed=None,
        ),
        MagicMock(
            id="id-2",
            title="Post 2",
            link="https://x.com/2",
            summary="s2",
            published_parsed=(2026, 4, 18, 11, 0, 0, 0, 0, 0),
            updated_parsed=None,
        ),
    ]
    with patch("feedparser.parse", return_value=fake_parsed):
        env = ActivityEnvironment()
        result = await env.run(
            rss.fetch_feed,
            FetchFeedInput(url="https://feed.example.com/rss"),
        )
    assert isinstance(result, FetchFeedResult)
    assert len(result.entries) == 2
    assert result.entries[0]["title"] == "Post 1"
    assert result.latest_published.startswith("2026-04-18T11")


@pytest.mark.asyncio
async def test_fetch_feed_respects_cursor(rss):
    fake_parsed = MagicMock()
    fake_parsed.entries = [
        MagicMock(
            id="old",
            title="Old",
            link="x",
            summary="",
            published_parsed=(2026, 4, 1, 0, 0, 0, 0, 0, 0),
            updated_parsed=None,
        ),
        MagicMock(
            id="new",
            title="New",
            link="y",
            summary="",
            published_parsed=(2026, 4, 20, 0, 0, 0, 0, 0, 0),
            updated_parsed=None,
        ),
    ]
    with patch("feedparser.parse", return_value=fake_parsed):
        env = ActivityEnvironment()
        result = await env.run(
            rss.fetch_feed,
            FetchFeedInput(url="https://x", since_cursor="2026-04-10T00:00:00"),
        )
    # Only "New" is after the cursor
    assert len(result.entries) == 1
    assert result.entries[0]["title"] == "New"
