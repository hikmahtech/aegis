"""What `record_feed_run` reports about a feed, and that the feed stats agree
(#511, from the audit): one definition of "tracking since" (the first entry
recorded, else the first poll), the last entry the store kept, and the good
fetches in a row that end a failure."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest_asyncio
from aegis.services import feeds
from aegis_worker.activities.rss import RssActivities
from temporalio.testing import ActivityEnvironment

FIRST_POLL = "2026-01-05T00:00:00+00:00"


@pytest_asyncio.fixture(loop_scope="function")
async def channel(db_pool):
    url = f"https://zztracking-{uuid.uuid4().hex[:8]}.test/feed.xml"
    cid = await db_pool.fetchval(
        "INSERT INTO channels (kind, identifier, config) VALUES ('rss', $1, $2) RETURNING id::text",
        url,
        {"label": "tracking", "tracking_since": FIRST_POLL},
    )
    yield db_pool, cid
    await db_pool.execute("DELETE FROM feed_entries WHERE channel_id = $1::uuid", cid)
    await db_pool.execute("DELETE FROM channels WHERE id = $1::uuid", cid)


async def _run(pool, cid: str, ok: bool = True) -> dict:
    outcome = {"ok": True} if ok else {"ok": False, "error": "HTTP 503"}
    return await ActivityEnvironment().run(RssActivities(db_pool=pool).record_feed_run, cid, outcome)


async def _stats(pool, cid: str) -> dict:
    return next(f for f in await feeds.feed_stats(pool) if f["id"] == cid)


async def test_without_entries_tracking_since_is_the_first_poll_everywhere(channel):
    pool, cid = channel
    run = await _run(pool, cid)
    assert run["tracking_since"] == FIRST_POLL
    assert run["last_stored_at"] is None
    assert (await _stats(pool, cid))["tracking_since"] == FIRST_POLL


async def test_with_entries_tracking_starts_at_the_first_and_stale_reads_the_last_stored(channel):
    pool, cid = channel
    for n, (mode, seen) in enumerate(
        [("full", datetime(2026, 2, 1, tzinfo=UTC)), ("abstract", datetime(2026, 3, 1, tzinfo=UTC)),
         ("failed", datetime(2026, 4, 1, tzinfo=UTC))]
    ):
        await pool.execute(
            "INSERT INTO feed_entries (channel_id, external_id, link, mode, published, seen_at) "
            "VALUES ($1::uuid, $2, $3, $4, '', $5)",
            cid,
            f"e{n}",
            f"https://zz.test/{n}",
            mode,
            seen,
        )
    run = await _run(pool, cid)
    assert datetime.fromisoformat(run["tracking_since"]) == datetime(2026, 2, 1, tzinfo=UTC)
    # A failed entry was seen, not stored.
    assert datetime.fromisoformat(run["last_stored_at"]) == datetime(2026, 3, 1, tzinfo=UTC)
    assert (await _stats(pool, cid))["tracking_since"] == run["tracking_since"]


async def test_good_fetches_count_in_a_row_and_a_failure_resets_them(channel):
    pool, cid = channel
    seen = [await _run(pool, cid, ok) for ok in (True, True, False, True)]
    assert [(r["fetch_successes"], r["fetch_failures"]) for r in seen] == [
        (1, 0), (2, 0), (0, 1), (1, 0)
    ]
