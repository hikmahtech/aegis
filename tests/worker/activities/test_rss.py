"""RssActivities tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from aegis_worker.activities import rss as rss_mod
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


@pytest.fixture(autouse=True)
def downloaded(monkeypatch):
    """The fetch itself — the guarded client, redirects, the size cap — is
    `test_fetch_guards.py`'s. Here it hands over a body, and the patched
    `feedparser.parse` decides what that body holds."""
    state = {"out": (b"<rss/>", {}, "")}

    async def fake(url: str, user_agent: str = ""):
        return state["out"]

    monkeypatch.setattr(rss_mod, "_download_feed", fake)
    return state


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
    assert result.entries[1]["published"].startswith("2026-04-18T11")


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


def _tied(*ids: str) -> MagicMock:
    """Entries that all share one timestamp, as an arXiv burst does (#584)."""
    parsed = MagicMock()
    parsed.entries = [
        MagicMock(
            id=i,
            title=i,
            link=f"https://x/{i}",
            summary="",
            published_parsed=(2026, 9, 12, 4, 0, 0, 0, 0, 0),
            updated_parsed=None,
        )
        for i in ids
    ]
    return parsed


async def _fetch_tied(rss, since_cursor_id):
    with patch("feedparser.parse", return_value=_tied("a", "b", "c")):
        result = await ActivityEnvironment().run(
            rss.fetch_feed,
            FetchFeedInput(
                url="https://x",
                since_cursor="2026-09-12T04:00:00+00:00",
                since_cursor_id=since_cursor_id,
            ),
        )
    return [e["id"] for e in result.entries]


@pytest.mark.asyncio
async def test_fetch_feed_keeps_tied_entries_past_the_cursor_id(rss):
    """The cursor is `(published, id)`: at the cursor's own timestamp, only
    the entries after its id are new."""
    assert await _fetch_tied(rss, "b") == ["c"]


@pytest.mark.asyncio
async def test_an_empty_cursor_id_offers_every_entry_at_the_cursor_timestamp(rss):
    """A channel whose cursor predates the id: "" is the lowest id, so the
    whole tied batch is offered once more."""
    assert await _fetch_tied(rss, "") == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_no_cursor_id_keeps_the_old_timestamp_rule(rss):
    """A run that started before the change passes no id: an entry is new
    only when its timestamp is later, exactly as before."""
    assert await _fetch_tied(rss, None) == []


# --------------------------------------------------------------------------
# #511 — a failed fetch says so. A dead URL, a 404 or an HTML page used to
# come back as an empty parse, which read as "quiet".
# --------------------------------------------------------------------------


def _empty_parse(**attrs):
    parsed = MagicMock()
    parsed.entries = []
    parsed.bozo = attrs.get("bozo", 0)
    parsed.bozo_exception = attrs.get("bozo_exception")
    parsed.version = attrs.get("version", "")
    return parsed


@pytest.mark.asyncio
async def test_fetch_feed_reports_a_failed_download_without_parsing(rss, downloaded):
    downloaded["out"] = (b"", {}, "HTTP 404")
    with patch("feedparser.parse") as parse:
        result = await ActivityEnvironment().run(rss.fetch_feed, FetchFeedInput(url="https://x"))
    assert result.entries == []
    assert result.error == "HTTP 404"
    parse.assert_not_called()


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
