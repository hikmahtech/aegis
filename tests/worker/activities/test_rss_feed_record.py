"""RssActivities' feed record (#511, #512), on real Postgres."""

from __future__ import annotations

import pytest
import pytest_asyncio
from aegis_worker.activities.intelligence import TRACKED_TOPICS_SETTING
from aegis_worker.activities.rss import RssActivities
from temporalio.testing import ActivityEnvironment

pytestmark = pytest.mark.asyncio

_URL = "https://zzfeedrec.test/feed.xml"


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    await db_pool.execute("DELETE FROM channels WHERE identifier = $1", _URL)
    yield db_pool
    await db_pool.execute("DELETE FROM channels WHERE identifier = $1", _URL)
    await db_pool.execute("DELETE FROM settings WHERE key = $1", TRACKED_TOPICS_SETTING)


async def _channel(pool) -> str:
    return await pool.fetchval(
        "INSERT INTO channels (kind, identifier, config) VALUES ('rss', $1, $2) RETURNING id::text",
        _URL,
        {"label": "rec"},
    )


async def test_gate_terms_are_the_intel_topics_then_the_tracked_ones_deduplicated(pool):
    await pool.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        TRACKED_TOPICS_SETTING,
        {"topics": [{"name": "crypto", "queries": ["bitcoin", "AI"]}]},
    )
    terms = await ActivityEnvironment().run(RssActivities(db_pool=pool).load_gate_terms)
    # The seeded intel scans carry "ai"; the tracked "AI" is the same term.
    assert "ai" in terms
    assert "bitcoin" in terms
    assert sum(1 for t in terms if t.lower() == "ai") == 1


async def test_record_feed_entries_upserts_so_a_retried_failure_reads_as_stored(pool):
    cid = await _channel(pool)
    act = RssActivities(db_pool=pool)
    env = ActivityEnvironment()
    n = await env.run(
        act.record_feed_entries,
        cid,
        [
            {"external_id": "e1", "link": "https://x/1", "content_id": None, "mode": "failed"},
            {"external_id": "e2", "link": "https://x/2", "content_id": "c2", "mode": "abstract"},
            {"external_id": "", "link": "no id is skipped"},
        ],
    )
    assert n == 2
    await env.run(
        act.record_feed_entries,
        cid,
        [{"external_id": "e1", "link": "https://x/1", "content_id": "c1", "mode": "full"}],
    )
    rows = {
        r["external_id"]: (r["mode"], r["content_id"])
        for r in await pool.fetch("SELECT * FROM feed_entries WHERE channel_id = $1::uuid", cid)
    }
    assert rows == {"e1": ("full", "c1"), "e2": ("abstract", "c2")}


async def test_record_feed_entries_ignores_a_channel_id_that_is_not_a_uuid(pool):
    n = await ActivityEnvironment().run(
        RssActivities(db_pool=pool).record_feed_entries, "ch-1", [{"external_id": "x"}]
    )
    assert n == 0


async def test_record_feed_run_counts_failures_in_a_row_and_a_success_resets(pool):
    cid = await _channel(pool)
    act = RssActivities(db_pool=pool)
    env = ActivityEnvironment()
    one = await env.run(act.record_feed_run, cid, {"ok": False, "error": "HTTP 503"})
    two = await env.run(act.record_feed_run, cid, {"ok": False, "error": "HTTP 503"})
    config = await pool.fetchval("SELECT config FROM channels WHERE id = $1::uuid", cid)
    assert (one["fetch_failures"], two["fetch_failures"]) == (1, 2)
    assert config["last_fetch_error"] == "HTTP 503"
    assert config["label"] == "rec"  # the rest of the config survives

    ok = await env.run(act.record_feed_run, cid, {"ok": True, "backlog": 7})
    config = await pool.fetchval("SELECT config FROM channels WHERE id = $1::uuid", cid)
    assert ok["fetch_failures"] == 0
    assert config["last_fetch_error"] == ""
    assert config["backlog"] == 7
    assert config["last_fetch_ok_at"]
