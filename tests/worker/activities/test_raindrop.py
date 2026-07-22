"""RaindropActivities — poll + cursor updates."""

from __future__ import annotations

import pytest
import respx
from aegis_worker.activities.raindrop import (
    _MAX_PAGES,
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


@pytest.mark.asyncio
@respx.mock
async def test_poll_bookmarks_paginates_across_pages(raindrop):
    """A full (50-item) first page must trigger a second-page fetch; a short
    second page (< 50 items) stops the loop. Both pages' items are collected.
    """

    def _responder(request):
        page = int(request.url.params.get("page", "0"))
        if page == 0:
            items = [
                {
                    "_id": i,
                    "link": f"https://x.com/p0-{i}",
                    "title": f"p0-{i}",
                    "excerpt": "",
                    "tags": [],
                    "created": f"2026-05-01T00:{i:02d}:00Z",
                }
                for i in range(50)
            ]
        elif page == 1:
            items = [
                {
                    "_id": 100 + i,
                    "link": f"https://x.com/p1-{i}",
                    "title": f"p1-{i}",
                    "excerpt": "",
                    "tags": [],
                    "created": f"2026-05-01T01:{i:02d}:00Z",
                }
                for i in range(10)
            ]
        else:
            items = []
        return Response(200, json={"items": items, "count": len(items)})

    route = respx.get("https://api.raindrop.io/rest/v1/raindrops/0").mock(
        side_effect=_responder
    )
    env = ActivityEnvironment()
    result = await env.run(
        raindrop.poll_bookmarks,
        PollBookmarksInput(since_cursor=None),
    )
    assert route.call_count == 2
    assert len(result.bookmarks) == 60
    assert result.latest_created == "2026-05-01T01:09:00Z"


@pytest.mark.asyncio
@respx.mock
async def test_poll_bookmarks_stops_at_cursor_even_mid_full_page(raindrop):
    """Hitting the cursor partway through a full (50-item) page must stop
    pagination immediately — no wasted page-2 request."""
    since_cursor = "2026-05-01T00:02:00Z"
    items = [
        {
            "_id": 1,
            "link": "https://x.com/new1",
            "title": "new1",
            "excerpt": "",
            "tags": [],
            "created": "2026-05-01T00:04:00Z",
        },
        {
            "_id": 2,
            "link": "https://x.com/new2",
            "title": "new2",
            "excerpt": "",
            "tags": [],
            "created": "2026-05-01T00:03:00Z",
        },
        # Everything from here on is at/before the cursor — never inspected
        # past the break, so minimal placeholder dicts are fine.
        *[{"created": "2020-01-01T00:00:00Z"} for _ in range(48)],
    ]
    assert len(items) == 50
    route = respx.get("https://api.raindrop.io/rest/v1/raindrops/0").mock(
        return_value=Response(200, json={"items": items, "count": 50})
    )
    env = ActivityEnvironment()
    result = await env.run(
        raindrop.poll_bookmarks,
        PollBookmarksInput(since_cursor=since_cursor),
    )
    assert route.call_count == 1
    assert [b["id"] for b in result.bookmarks] == ["1", "2"]


@pytest.mark.asyncio
@respx.mock
async def test_poll_bookmarks_respects_page_cap(raindrop):
    """A feed that never returns a short page and never reaches the cursor
    (e.g. a misbehaving API) must stop at the page cap rather than looping
    forever."""

    def _full_page(request):
        page = int(request.url.params.get("page", "0"))
        items = [
            {
                "_id": page * 50 + i,
                "link": f"https://x.com/{page}-{i}",
                "title": "t",
                "excerpt": "",
                "tags": [],
                "created": f"2026-05-01T00:{i:02d}:00Z",
            }
            for i in range(50)
        ]
        return Response(200, json={"items": items, "count": 50})

    route = respx.get("https://api.raindrop.io/rest/v1/raindrops/0").mock(
        side_effect=_full_page
    )
    env = ActivityEnvironment()
    result = await env.run(
        raindrop.poll_bookmarks,
        PollBookmarksInput(since_cursor=None),
    )
    assert route.call_count == _MAX_PAGES
    assert len(result.bookmarks) == _MAX_PAGES * 50
