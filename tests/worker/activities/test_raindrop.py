"""RaindropActivities — poll + cursor updates."""

from __future__ import annotations

import pytest
import respx
from aegis_worker.activities.raindrop import (
    PollBookmarksInput,
    PollBookmarksResult,
    RaindropActivities,
)
from httpx import Response
from temporalio.testing import ActivityEnvironment


@pytest.fixture
def raindrop():
    return RaindropActivities(raindrop_api_token="tok-xyz")


@pytest.mark.asyncio
@respx.mock
async def test_poll_bookmarks_returns_items(raindrop):
    respx.get("https://api.raindrop.io/rest/v1/raindrops/0").mock(
        return_value=Response(
            200,
            json={
                "items": [
                    {
                        "_id": 1,
                        "link": "https://x.com/a",
                        "title": "A",
                        "excerpt": "snippet a",
                        "tags": ["rust"],
                        "created": "2026-04-18T10:00:00Z",
                    },
                    {
                        "_id": 2,
                        "link": "https://x.com/b",
                        "title": "B",
                        "excerpt": "",
                        "tags": [],
                        "created": "2026-04-18T11:00:00Z",
                    },
                ],
                "count": 2,
            },
        )
    )
    env = ActivityEnvironment()
    result = await env.run(
        raindrop.poll_bookmarks,
        PollBookmarksInput(since_cursor=None),
    )
    assert isinstance(result, PollBookmarksResult)
    assert len(result.bookmarks) == 2
    assert result.bookmarks[0]["id"] == "1"
    assert result.bookmarks[0]["title"] == "A"
    assert result.bookmarks[0]["tags"] == ["rust"]
    assert result.latest_created == "2026-04-18T11:00:00Z"


@pytest.mark.asyncio
@respx.mock
async def test_poll_bookmarks_applies_cursor_filter(raindrop):
    route = respx.get("https://api.raindrop.io/rest/v1/raindrops/0").mock(
        return_value=Response(200, json={"items": [], "count": 0})
    )
    env = ActivityEnvironment()
    await env.run(
        raindrop.poll_bookmarks,
        PollBookmarksInput(since_cursor="2026-04-10T00:00:00Z"),
    )
    assert route.called
    # Cursor-based search filter should be in query string (URL-encoded form)
    url = str(route.calls.last.request.url)
    assert "created" in url and ("2026-04-10" in url)


@pytest.mark.asyncio
@respx.mock
async def test_poll_bookmarks_drops_same_day_already_seen_items(raindrop):
    """Raindrop's `created:>YYYY-MM-DD` search returns same-day items, so
    a cursor mid-day re-pulls already-ingested rows. Post-fetch filter
    keeps only items strictly newer than the full-ISO cursor.
    """
    respx.get("https://api.raindrop.io/rest/v1/raindrops/0").mock(
        return_value=Response(
            200,
            json={
                "items": [
                    {
                        "_id": 100,
                        "link": "https://x.com/new",
                        "title": "Newer",
                        "excerpt": "",
                        "tags": [],
                        "created": "2026-05-25T14:00:00.000Z",
                    },
                    {
                        "_id": 99,
                        "link": "https://x.com/same-as-cursor",
                        "title": "Same as cursor",
                        "excerpt": "",
                        "tags": [],
                        "created": "2026-05-25T10:05:33.670Z",
                    },
                    {
                        "_id": 98,
                        "link": "https://x.com/earlier-same-day",
                        "title": "Earlier same day",
                        "excerpt": "",
                        "tags": [],
                        "created": "2026-05-25T08:00:00.000Z",
                    },
                ],
                "count": 3,
            },
        )
    )
    env = ActivityEnvironment()
    result = await env.run(
        raindrop.poll_bookmarks,
        PollBookmarksInput(since_cursor="2026-05-25T10:05:33.670Z"),
    )
    assert [b["id"] for b in result.bookmarks] == ["100"]
    assert result.latest_created == "2026-05-25T14:00:00.000Z"


@pytest.mark.asyncio
async def test_poll_with_empty_token_returns_empty():
    r = RaindropActivities(raindrop_api_token="")
    env = ActivityEnvironment()
    result = await env.run(
        r.poll_bookmarks,
        PollBookmarksInput(since_cursor=None),
    )
    assert result.bookmarks == []
    assert result.latest_created is None
