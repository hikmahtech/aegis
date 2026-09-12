"""A quiet feed is not a failing one (#511 validation).

feedparser marks an empty but recognised feed `bozo` for benign complaints
(a CharacterEncodingOverride, say). Three of those in a row raised a
`feed_failing` task for a feed that was merely quiet. And a feed that never
gave a dated entry needs a date to be judged stale from: when polling began.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest_asyncio
from aegis_worker.activities.rss import RssActivities, _fetch_error
from temporalio.testing import ActivityEnvironment

_URL = "https://zzquietfeed.test/feed.xml"


def _parse(**attrs):
    parsed = MagicMock()
    parsed.entries = []
    parsed.status = attrs.get("status", 200)
    parsed.bozo = attrs.get("bozo", 0)
    parsed.bozo_exception = attrs.get("bozo_exception")
    parsed.version = attrs.get("version", "")
    return parsed


def test_a_recognised_feed_that_is_empty_with_a_benign_complaint_is_quiet():
    exc = Exception("document declared as us-ascii, but parsed as utf-8")
    assert _fetch_error(_parse(bozo=1, bozo_exception=exc, version="rss20")) == ""


def test_a_bozo_parse_that_is_not_a_feed_is_still_a_failure():
    exc = ValueError("not well-formed (invalid token)")
    assert "not well-formed" in _fetch_error(_parse(bozo=1, bozo_exception=exc))


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    await db_pool.execute("DELETE FROM channels WHERE identifier = $1", _URL)
    yield db_pool
    await db_pool.execute("DELETE FROM channels WHERE identifier = $1", _URL)


async def test_record_feed_run_notes_when_polling_began_once(pool):
    cid = await pool.fetchval(
        "INSERT INTO channels (kind, identifier, config) VALUES ('rss', $1, $2) RETURNING id::text",
        _URL,
        {"label": "quiet"},
    )
    act = RssActivities(db_pool=pool)
    env = ActivityEnvironment()
    await env.run(act.record_feed_run, cid, {"ok": True})
    first = (await pool.fetchval("SELECT config FROM channels WHERE id = $1::uuid", cid))[
        "tracking_since"
    ]
    await env.run(act.record_feed_run, cid, {"ok": False, "error": "HTTP 503"})
    config = await pool.fetchval("SELECT config FROM channels WHERE id = $1::uuid", cid)
    assert first
    assert config["tracking_since"] == first, "tracking_since is set once and never moves"
    assert config["label"] == "quiet"
