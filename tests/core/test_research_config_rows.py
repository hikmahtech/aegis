"""The research lane's config rows (`feeds_config`, `research_config`,
`library_config`, `research_topics_config`): lenient read, strict write, the
shared cache, and the readers that changed behaviour with them.

Every default here is what the module constants said before the rows
existed, so a deployment with no row behaves as it did.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from aegis.services import (
    feeds,
    feeds_config,
    library,
    library_config,
    research_config,
    topics_config,
)
from aegis.services import research as rs
from aegis.services.config_rows import SettingsRow
from aegis.services.user_agent import bot_user_agent

from tests.core.test_library_service import BOOK, FakeConn, make_epub

ALL_ROWS = (feeds_config.ROW, research_config.ROW, library_config.ROW, topics_config.ROW)


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    keys = [r.key for r in ALL_ROWS]
    await db_pool.execute("DELETE FROM settings WHERE key = ANY($1::text[])", keys)
    for r in ALL_ROWS:
        r.clear_cache()
    yield db_pool
    await db_pool.execute("DELETE FROM settings WHERE key = ANY($1::text[])", keys)
    for r in ALL_ROWS:
        r.clear_cache()


# --------------------------------------------------------------------------
# merge is lenient, validate is strict — for each of the four rows
# --------------------------------------------------------------------------


def test_the_defaults_are_the_old_constants():
    assert feeds_config.merge(None) == {
        "failing_after": 3,
        "recovered_after": 2,
        "stale_after_days": 30,
        "unused_after_days": 90,
        "stale_review_hour": 3,
        "default_ingest": "full",
    }
    r = research_config.merge(None)
    assert r["wait_seconds"] == 45 and rs.RESEARCH_WAIT_S == 45
    assert r["depths"] == {
        "quick": {"pages": 3, "web_results": 8, "papers": 5},
        "thorough": {"pages": 6, "web_results": 15, "papers": 10},
    }
    assert (r["page_chars"], r["report_chars"], r["knowledge_hits"], r["note_hits"]) == (
        6000, 8000, 5, 3
    )
    lib = library_config.merge(None)
    assert (lib["read_chars"], lib["passages"], lib["passage_chars"], lib["pdf_default_pages"]) == (
        12000, 4, 1200, 5
    )
    assert (lib["research_book_hits"], lib["research_passage_min_similarity"]) == (3, 0.5)
    assert (lib["research_passage_chars"], lib["research_pdf_scan_pages"]) == (3000, 60)
    assert "chapter" in lib["stopwords"]
    assert topics_config.merge(None) == {
        "attention": {"high": 2, "medium": 3, "low": 5},
        "digest_items": 10,
        "brief_items": 7,
        "weekly_day": 6,
    }


def test_merge_keeps_the_default_for_every_bad_field_and_never_raises():
    m = feeds_config.merge(
        {"failing_after": "lots", "recovered_after": 0, "stale_review_hour": 25,
         "default_ingest": "GATE", "unused_after_days": 7}
    )
    assert (m["failing_after"], m["recovered_after"], m["stale_review_hour"]) == (3, 2, 3)
    assert (m["default_ingest"], m["unused_after_days"]) == ("gate", 7)
    assert feeds_config.merge("not an object") == feeds_config.merge(None)
    r = research_config.merge({"depths": {"quick": {"pages": 1}}, "academic_terms": ["thesis", 3]})
    assert r["depths"]["quick"] == {"pages": 1, "web_results": 8, "papers": 5}
    assert r["depths"]["thorough"] == research_config.DEFAULTS["depths"]["thorough"]
    assert r["academic_terms"] == ["thesis"]
    lib = library_config.merge({"passages": True, "stopwords": ["The", " and "]})
    assert lib["passages"] == 4 and lib["stopwords"] == ["the", "and"]
    t = topics_config.merge(
        {"attention": {"high": 1, "urgent": 9}, "digest_items": -1, "brief_items": "x", "weekly_day": 7}
    )
    assert t == {
        "attention": {"high": 1, "medium": 3, "low": 5},
        "digest_items": 10,
        "brief_items": 7,
        "weekly_day": 6,
    }


@pytest.mark.parametrize(
    ("validate", "bad", "message"),
    [
        (feeds_config.validate, {"failing_after": 0}, "failing_after"),
        (feeds_config.validate, {"stale_review_hour": 24}, "stale_review_hour"),
        (feeds_config.validate, {"default_ingest": "everything"}, "default_ingest"),
        (feeds_config.validate, ["x"], "object"),
        (research_config.validate, {"wait_seconds": 1}, "wait_seconds"),
        (research_config.validate, {"depths": {"quick": {"pages": -1}}}, "pages"),
        (research_config.validate, {"academic_terms": ["("]}, "not a valid pattern"),
        (research_config.validate, {"academic_terms": "papers"}, "list of strings"),
        (library_config.validate, {"read_chars": 100}, "read_chars"),
        (library_config.validate, {"research_passage_min_similarity": 2}, "between"),
        (topics_config.validate, {"attention": {"urgent": 1}}, "unknown priority"),
        (topics_config.validate, {"attention": {"high": 0}}, "high"),
        (topics_config.validate, {"digest_items": "ten"}, "digest_items"),
        (topics_config.validate, {"brief_items": 0}, "brief_items"),
        (topics_config.validate, {"weekly_day": 7}, "weekly_day"),
    ],
)
def test_validate_refuses_what_merge_would_silently_drop(validate, bad, message):
    with pytest.raises(ValueError, match=message):
        validate(bad)


def test_validate_fills_the_rest_from_the_defaults_and_normalises():
    out = feeds_config.validate({"default_ingest": " Abstract "})
    assert out == {**feeds_config.DEFAULTS, "default_ingest": "abstract"}
    out = topics_config.validate({"attention": {"low": 9}})
    assert out["attention"] == {"high": 2, "medium": 3, "low": 9}
    assert library_config.validate({"stopwords": ["The"]})["stopwords"] == ["the"]


# --------------------------------------------------------------------------
# the row: get/save/cache
# --------------------------------------------------------------------------


async def test_the_row_is_read_leniently_saved_strictly_and_cached(pool):
    await pool.execute(
        "INSERT INTO settings (key, value) VALUES ($1, $2)",
        feeds_config.SETTINGS_KEY,
        {"failing_after": 5, "recovered_after": "two"},
    )
    got = await feeds_config.get_feeds_config(pool)
    assert (got["failing_after"], got["recovered_after"]) == (5, 2)
    # Cached: a direct DB change is not seen until the cache clears or saves.
    await pool.execute(
        "UPDATE settings SET value = $2 WHERE key = $1",
        feeds_config.SETTINGS_KEY,
        {"failing_after": 6},
    )
    assert (await feeds_config.get_feeds_config(pool))["failing_after"] == 5
    assert (await feeds_config.ROW.get(pool, fresh=True))["failing_after"] == 6
    with pytest.raises(ValueError):
        await feeds_config.save_feeds_config(pool, {"failing_after": "lots"})
    assert (await feeds_config.ROW.get(pool, fresh=True))["failing_after"] == 6, "nothing written"
    saved = await feeds_config.save_feeds_config(pool, {"failing_after": 4})
    assert saved["failing_after"] == 4
    assert (await feeds_config.get_feeds_config(pool))["failing_after"] == 4, "save clears the cache"


async def test_no_pool_and_an_unreadable_row_both_read_as_the_defaults():
    row = SettingsRow("zz_never", feeds_config.merge, feeds_config.validate)
    assert await row.get(None) == feeds_config.DEFAULTS

    class Broken:
        async def fetchval(self, *a):
            raise RuntimeError("db down")

    assert await row.get(Broken(), fresh=True) == feeds_config.DEFAULTS


# --------------------------------------------------------------------------
# the readers: a non-default value changes what they do
# --------------------------------------------------------------------------


def test_research_limits_are_read_from_the_config():
    cfg = research_config.merge({"depths": {"quick": {"pages": 1, "web_results": 2, "papers": 0}}})
    assert rs.depth_limits(cfg, "quick") == {"pages": 1, "web_results": 2, "papers": 0}
    assert rs.depth_limits(None, "thorough") == {"pages": 6, "web_results": 15, "papers": 10}
    assert rs.depth_limits(cfg, "nonsense") == {"pages": 3, "web_results": 8, "papers": 5}
    assert rs.looks_academic("any recent papers on RAG?") is True
    assert rs.looks_academic("any recent papers on RAG?", terms=["thesis"]) is False
    assert rs.looks_academic("my thesis on RAG", terms=["thesis"]) is True
    assert rs.looks_academic("q", ["arxiv.org"], terms=[]) is True, "a paper domain still counts"
    assert rs.looks_academic("papers", terms=["("]) is False, "a bad pattern is skipped, not raised"


def test_the_synthesis_prompt_names_the_owning_agent_or_nobody():
    assert rs.synthesis_system("Raphael").startswith("You are Raphael, a careful research analyst.")
    assert rs.synthesis_system("") == rs.SYNTHESIS_SYSTEM
    assert "You are a careful research analyst." in rs.SYNTHESIS_SYSTEM
    assert "untrusted" in rs.synthesis_system("X").lower()


def test_the_semantic_scholar_key_is_sent_only_when_set():
    assert rs.s2_headers("") == {} and rs.s2_headers("  ") == {}
    assert rs.s2_headers("k1") == {"x-api-key": "k1"}


def test_the_bot_user_agent_names_the_contact_url_when_there_is_one():
    from types import SimpleNamespace

    assert bot_user_agent(None) == "AegisBot/2.0"
    assert bot_user_agent(SimpleNamespace(aegis_ui_url="https://aegis.example")) == (
        "AegisBot/2.0 (+https://aegis.example)"
    )
    assert bot_user_agent(
        SimpleNamespace(aegis_ui_url="https://aegis.example", bot_contact_url="https://c.example/bot")
    ) == "AegisBot/2.0 (+https://c.example/bot)"
    # A LAN-only link host is no contact for the outside world.
    assert bot_user_agent(
        SimpleNamespace(aegis_ui_url="https://aegis-lan.example", aegis_public_url="https://aegis.example")
    ) == "AegisBot/2.0 (+https://aegis.example)"


def test_feed_readers_take_the_row_default_and_the_feed_still_wins():
    assert feeds.ingest_mode({}, "abstract") == "abstract"
    assert feeds.ingest_mode({"ingest": "gate"}, "abstract") == "gate"
    assert feeds.ingest_mode({}, "nonsense") == "full"
    assert feeds.stale_after_days({}, 7) == 7
    assert feeds.stale_after_days({"stale_after_days": 3}, 7) == 3
    assert feeds.stale_after_days({}, 0) == 30


async def test_feed_stats_and_unused_feeds_use_the_configured_window(pool):
    from tests.core.test_feeds_service import _channel, _entry

    await pool.execute("DELETE FROM channels WHERE identifier LIKE 'https://zzfeeds.test/%'")
    ch = await _channel(pool, "cfg-old", {"label": "Cfg old"})
    await _entry(pool, ch, "x1", "zzfeeds-cfg1", "full", 40)
    try:
        assert "Cfg old" not in {f["label"] for f in await feeds.unused_feeds(pool)}
        await feeds_config.save_feeds_config(pool, {"unused_after_days": 30})
        assert "Cfg old" in {f["label"] for f in await feeds.unused_feeds(pool)}
        row = next(f for f in await feeds.feed_stats(pool) if f["label"] == "Cfg old")
        assert row["entries_90d"] == 0, "the long window is now 30 days"
        assert row["stale_after_days"] == 30
        await feeds_config.save_feeds_config(pool, {"stale_after_days": 9, "default_ingest": "gate"})
        row = next(f for f in await feeds.feed_stats(pool) if f["label"] == "Cfg old")
        assert (row["stale_after_days"], row["ingest"]) == (9, "gate")
        assert "agent_id" not in row
    finally:
        await pool.execute("DELETE FROM channels WHERE identifier LIKE 'https://zzfeeds.test/%'")


async def test_the_vector_size_is_read_from_the_column(pool):
    assert await feeds.vector_bytes(pool) == 768 * 4


async def test_subscribe_uses_the_configured_default_ingest_mode(pool, monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        feeds, "inspect_feed", AsyncMock(return_value={"ok": True, "title": "T", "entries": 2})
    )
    url = "https://zzfeeds.test/cfg-sub"
    await pool.execute("DELETE FROM channels WHERE identifier = $1", url)
    try:
        await feeds_config.save_feeds_config(pool, {"default_ingest": "abstract"})
        out = await feeds.subscribe(pool, url)
        assert out["status"] == "subscribed"
        cfg = await pool.fetchval("SELECT config FROM channels WHERE identifier = $1", url)
        assert cfg["ingest"] == "abstract" and "agent_id" not in cfg
    finally:
        await pool.execute("DELETE FROM channels WHERE identifier = $1", url)


@pytest.mark.asyncio
async def test_library_limits_change_a_read():
    conn = FakeConn(BOOK, make_epub())
    default = await library.read_book(conn, 12, query="what does the learning rate do")
    limits = library_config.merge({"passages": 1, "passage_chars": 300})
    one = await library.read_book(conn, 12, query="what does the learning rate do", limits=limits)
    assert len(default["passages"]) >= 2 and len(one["passages"]) == 1
    assert len(one["passages"][0]["text"]) <= 300
    # A stopword list that swallows the whole query finds nothing.
    stop = library_config.merge({"stopwords": ["what", "does", "the", "learning", "rate"]})
    none = await library.read_book(conn, 12, query="what does the learning rate do", limits=stop)
    assert none["passages"] == []
    # `read_chars` is the default clip when the caller names none.
    short = await library.read_book(conn, 12, limits=library_config.merge({"read_chars": 600}))
    assert short["truncated"] is True and len(short["text"]) <= 600
    assert library.query_terms("The book chapter", ["the"]) == {"book", "chapter"}


def test_topic_thresholds_come_from_the_config_unless_the_topic_sets_its_own():
    from aegis.services.research_topics import Topic, parse_topics, validate_registry

    cfg = topics_config.merge({"attention": {"high": 7}})
    assert Topic("A", (), "high").threshold_for(cfg) == 7
    assert Topic("A", (), "high").threshold == 2, "the code default without a row"
    assert Topic("A", (), "medium", 1).threshold_for(cfg) == 1
    parsed = parse_topics({"topics": [{"name": "A", "threshold": 4}, {"name": "B", "threshold": "x"}]})
    assert [t.threshold_override for t in parsed] == [4, None]
    assert parsed[0].as_entry() == {"name": "A", "queries": [], "priority": "medium", "threshold": 4}
    with pytest.raises(ValueError, match="threshold"):
        validate_registry({"topics": [{"name": "A", "threshold": 0}]})
    with pytest.raises(ValueError, match="twice"):
        validate_registry({"topics": [{"name": "A"}, {"name": "a"}]})
    with pytest.raises(ValueError, match="priority"):
        validate_registry({"topics": [{"name": "A", "priority": "urgent"}]})
    assert validate_registry({"topics": [{"name": " A ", "queries": ["x", " "], "threshold": ""}]}) == [
        {"name": "A", "queries": ["x"], "priority": "medium"}
    ]
