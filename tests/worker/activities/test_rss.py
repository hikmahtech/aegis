"""RssActivities tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from aegis_worker.activities.rss import (
    FetchFeedInput,
    FetchFeedResult,
    RssActivities,
    gate_pattern,
    passes_gate,
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


# --------------------------------------------------------------------------
# #511 — a failed fetch says so. feedparser never raises: a dead URL, a 404
# or an HTML page come back as an empty parse, which used to read as "quiet".
# --------------------------------------------------------------------------


def _empty_parse(**attrs):
    parsed = MagicMock()
    parsed.entries = []
    parsed.status = attrs.get("status", 200)
    parsed.bozo = attrs.get("bozo", 0)
    parsed.bozo_exception = attrs.get("bozo_exception")
    return parsed


@pytest.mark.asyncio
async def test_fetch_feed_reports_an_http_error(rss):
    with patch("feedparser.parse", return_value=_empty_parse(status=404)):
        result = await ActivityEnvironment().run(rss.fetch_feed, FetchFeedInput(url="https://x"))
    assert result.entries == []
    assert result.error == "HTTP 404"


@pytest.mark.asyncio
async def test_fetch_feed_reports_a_response_that_is_not_a_feed(rss):
    parsed = _empty_parse(bozo=1, bozo_exception=ValueError("not well-formed (invalid token)"))
    with patch("feedparser.parse", return_value=parsed):
        result = await ActivityEnvironment().run(rss.fetch_feed, FetchFeedInput(url="https://x"))
    assert "not well-formed" in result.error


@pytest.mark.asyncio
async def test_an_empty_feed_is_not_an_error(rss):
    with patch("feedparser.parse", return_value=_empty_parse()):
        result = await ActivityEnvironment().run(rss.fetch_feed, FetchFeedInput(url="https://x"))
    assert result.error == ""


# --------------------------------------------------------------------------
# #512 — the relevance gate: a topic term, as a whole word, in the title or
# summary. No LLM.
# --------------------------------------------------------------------------


def test_the_gate_matches_whole_words_in_any_case():
    pattern = gate_pattern(["ai", "c++", "machine learning"])
    assert passes_gate(pattern, {"title": "New AI agents", "summary": ""})
    assert passes_gate(pattern, {"title": "x", "summary": "Learn C++ in a week"})
    assert passes_gate(pattern, {"title": "Machine Learning at scale", "summary": ""})
    # "ai" inside a word is not the topic.
    assert not passes_gate(pattern, {"title": "Chair design", "summary": "Paint the stairs"})


def test_no_terms_means_the_gate_lets_everything_through():
    assert gate_pattern([]) is None
    assert gate_pattern(["", "  "]) is None
    assert passes_gate(None, {"title": "anything", "summary": ""})
