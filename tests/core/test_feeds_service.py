"""services/feeds.py — the feed list and what each feed is worth (#511, #512).

Real Postgres throughout: the stats are one SQL query over `feed_entries` and
`knowledge_injection_log`, and a mock would only test the mock.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest_asyncio
import respx
from aegis.services import feeds
from httpx import Response

_PREFIX = "https://zzfeeds.test/"
_THREAD = "zzfeeds-thread"

_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Example &amp; Co</title>
<item><title>One</title><link>https://zzfeeds.test/1</link></item>
<item><title>Two</title><link>https://zzfeeds.test/2</link></item>
</channel></rss>"""

_HTML_WITH_FEED = """<html><head><title>A blog</title>
<link rel="alternate" type="application/rss+xml" href="/feed.xml">
</head><body>hello</body></html>"""


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    async def clean():
        await db_pool.execute("DELETE FROM knowledge_injection_log WHERE thread_id = $1", _THREAD)
        # feed_entries go with their channel (ON DELETE CASCADE).
        await db_pool.execute("DELETE FROM channels WHERE identifier LIKE $1", _PREFIX + "%")
        await db_pool.execute(
            "DELETE FROM knowledge_chunks WHERE content_id LIKE 'zzfeeds-%'"
        )
        await db_pool.execute("DELETE FROM knowledge_content WHERE content_id LIKE 'zzfeeds-%'")

    await clean()
    yield db_pool
    await clean()


async def _channel(pool, name: str, config: dict | None = None, active: bool = True) -> str:
    return await pool.fetchval(
        "INSERT INTO channels (kind, identifier, config, active) "
        "VALUES ('rss', $1, $2, $3) RETURNING id::text",
        _PREFIX + name,
        config if config is not None else {"label": name},
        active,
    )


async def _entry(pool, cid, ext, content_id, mode="full", days_ago=1):
    await pool.execute(
        "INSERT INTO feed_entries (channel_id, external_id, link, content_id, mode, seen_at) "
        "VALUES ($1::uuid, $2, $2, $3, $4, now() - make_interval(days => $5))",
        cid,
        ext,
        content_id,
        mode,
        days_ago,
    )


async def _used(pool, content_ids: list[str], days_ago: int = 1):
    await pool.execute(
        "INSERT INTO knowledge_injection_log (agent_id, thread_id, source, content_ids, created_at) "
        "VALUES ('raphael', $1, 'chat', $2, now() - make_interval(days => $3))",
        _THREAD,
        content_ids,
        days_ago,
    )


def _by_id(rows: list[dict], cid: str) -> dict:
    return next(r for r in rows if r["id"] == cid)


# --------------------------------------------------------------------------
# The pure helpers
# --------------------------------------------------------------------------


def test_ingest_mode_defaults_to_full_on_anything_unknown():
    assert feeds.ingest_mode({"ingest": "abstract"}) == "abstract"
    assert feeds.ingest_mode({"ingest": " GATE "}) == "gate"
    assert feeds.ingest_mode({"ingest": "everything"}) == "full"
    assert feeds.ingest_mode(None) == "full"


def test_stale_after_days_falls_back_on_a_bad_value():
    assert feeds.stale_after_days({"stale_after_days": 7}) == 7
    assert feeds.stale_after_days({"stale_after_days": "soon"}) == feeds.DEFAULT_STALE_AFTER_DAYS
    assert feeds.stale_after_days({"stale_after_days": 0}) == feeds.DEFAULT_STALE_AFTER_DAYS


def test_a_feed_is_called_by_its_label_else_its_host():
    assert feeds.feed_label("https://arxiv.org/rss/cs.AI", {"label": "arxiv-ai"}) == "arxiv-ai"
    assert feeds.feed_label("https://arxiv.org/rss/cs.AI", {}) == "arxiv.org"


# --------------------------------------------------------------------------
# feed_stats / unused_feeds
# --------------------------------------------------------------------------


async def test_feed_stats_counts_entries_stored_abstracts_and_use(pool):
    a = await _channel(
        pool,
        "a",
        {"label": "Feed A", "ingest": "gate", "last_cursor": "2026-09-10T00:00:00+00:00",
         "fetch_failures": 2, "last_fetch_error": "HTTP 503", "backlog": 12},
    )
    b = await _channel(pool, "b")
    await _entry(pool, a, "e1", "zzfeeds-c1", "full", 1)
    await _entry(pool, a, "e2", "zzfeeds-c2", "abstract", 2)
    await _entry(pool, a, "e3", None, "failed", 3)
    await _entry(pool, a, "e4", "zzfeeds-c4", "full", 60)
    await _used(pool, ["zzfeeds-c1"], days_ago=2)
    await _used(pool, ["zzfeeds-c4", "zzfeeds-someone-else"], days_ago=50)

    rows = await feeds.feed_stats(pool)
    fa, fb = _by_id(rows, a), _by_id(rows, b)
    assert fa["label"] == "Feed A"
    assert fa["ingest"] == "gate"
    assert (fa["entries_30d"], fa["entries_90d"]) == (3, 4)
    assert (fa["stored_30d"], fa["stored_90d"]) == (2, 3)
    assert fa["abstract_30d"] == 1
    assert (fa["used_30d"], fa["used_90d"]) == (1, 2)
    assert fa["fetch_failures"] == 2
    assert fa["last_fetch_error"] == "HTTP 503"
    assert fa["backlog"] == 12
    assert fa["last_entry_at"] == "2026-09-10T00:00:00+00:00"
    assert fa["tracking_since"] is not None
    # A feed with no history yet is listed, with zeros and no tracking date.
    assert (fb["entries_30d"], fb["used_90d"], fb["tracking_since"]) == (0, 0, None)
    assert fb["ingest"] == "full"


async def test_unused_feeds_needs_90_days_of_history_and_no_use(pool):
    old_unused = await _channel(pool, "old-unused", {"label": "Old unused"})
    await _entry(pool, old_unused, "x1", "zzfeeds-o1", "full", 120)
    young = await _channel(pool, "young", {"label": "Young"})
    await _entry(pool, young, "y1", "zzfeeds-y1", "full", 10)
    old_used = await _channel(pool, "old-used", {"label": "Old used"})
    await _entry(pool, old_used, "u1", "zzfeeds-u1", "full", 120)
    await _used(pool, ["zzfeeds-u1"], days_ago=10)
    inactive = await _channel(pool, "inactive", {"label": "Inactive"}, active=False)
    await _entry(pool, inactive, "i1", "zzfeeds-i1", "full", 120)

    labels = {f["label"] for f in await feeds.unused_feeds(pool)}
    assert "Old unused" in labels
    assert not labels & {"Young", "Old used", "Inactive"}


# --------------------------------------------------------------------------
# subscribe / unsubscribe
# --------------------------------------------------------------------------


@respx.mock
async def test_subscribe_checks_the_feed_then_adds_and_resubscribes(pool):
    url = _PREFIX + "feed.xml"
    respx.get(url).mock(return_value=Response(200, text=_RSS))
    with patch.object(feeds, "public_url_problem", AsyncMock(return_value=None)):
        first = await feeds.subscribe(pool, url, agent_id="raphael")
        again = await feeds.subscribe(pool, url)
        dropped = await feeds.unsubscribe(pool, "example & co")
        back = await feeds.subscribe(pool, url)

    assert first["status"] == "subscribed"
    assert first["label"] == "Example & Co"
    assert first["entries_in_feed"] == 2
    row = await pool.fetchrow("SELECT config, active FROM channels WHERE identifier = $1", url)
    assert row["config"]["agent_id"] == "raphael"
    assert row["config"]["ingest"] == "full"
    assert again["status"] == "already_subscribed"
    assert dropped["status"] == "unsubscribed"
    assert back["status"] == "resubscribed"
    assert (await pool.fetchval("SELECT active FROM channels WHERE identifier = $1", url)) is True


@respx.mock
async def test_subscribe_refuses_a_web_page_and_says_where_its_feed_is(pool):
    url = _PREFIX + "blog"
    respx.get(url).mock(
        return_value=Response(200, text=_HTML_WITH_FEED, headers={"content-type": "text/html"})
    )
    with patch.object(feeds, "public_url_problem", AsyncMock(return_value=None)):
        out = await feeds.subscribe(pool, url)
    assert "not an RSS or Atom feed" in out["error"]
    assert out["suggest"] == _PREFIX + "feed.xml"
    assert await pool.fetchval("SELECT count(*) FROM channels WHERE identifier = $1", url) == 0


async def test_subscribe_refuses_a_non_public_host(pool):
    out = await feeds.subscribe(pool, "http://localhost:8080/feed.xml")
    assert "not a public host" in out["error"]


async def test_unsubscribe_names_the_feeds_when_nothing_matches_or_several_do(pool):
    await _channel(pool, "p", {"label": "Twin"})
    await _channel(pool, "q", {"label": "twin"})
    missing = await feeds.unsubscribe(pool, "no such feed")
    both = await feeds.unsubscribe(pool, "Twin")
    assert "no active feed matches" in missing["error"]
    assert "matches 2 feeds" in both["error"]
    assert sorted(both["feeds"]) == [_PREFIX + "p", _PREFIX + "q"]


# --------------------------------------------------------------------------
# retention_preview — a dry run
# --------------------------------------------------------------------------


async def _pdf(pool, cid: str, chunks: int, days_ago: int):
    await pool.execute(
        "INSERT INTO knowledge_content (content_id, url, title, source_type, ingested_at) "
        "VALUES ($1, $2, $1, 'pdf', now() - make_interval(days => $3))",
        cid,
        f"https://zzfeeds.test/{cid}.pdf",
        days_ago,
    )
    for i in range(chunks):
        await pool.execute(
            "INSERT INTO knowledge_chunks (content_id, chunk_index, chunk_text) VALUES ($1, $2, $3)",
            cid,
            i,
            "x" * 100,
        )


async def test_retention_preview_counts_old_unused_pdfs_and_changes_nothing(pool):
    before = await feeds.retention_preview(pool, 30)
    await _pdf(pool, "zzfeeds-old", 5, 40)  # counted: old, never used
    await _pdf(pool, "zzfeeds-used", 5, 40)  # a prompt used it
    await _pdf(pool, "zzfeeds-new", 5, 5)  # too young
    await _used(pool, ["zzfeeds-used"], days_ago=1)

    after = await feeds.retention_preview(pool, 30)
    assert after["dry_run"] is True
    assert after["documents"] - before["documents"] == 1
    assert after["chunks"] - before["chunks"] == 5
    assert after["chunks_removed"] - before["chunks_removed"] == 4
    assert after["text_bytes_freed"] - before["text_bytes_freed"] == 400
    assert after["vector_bytes_freed"] - before["vector_bytes_freed"] == 4 * 768 * 4
    # Nothing was touched.
    assert await pool.fetchval(
        "SELECT count(*) FROM knowledge_chunks WHERE content_id = 'zzfeeds-old'"
    ) == 5
