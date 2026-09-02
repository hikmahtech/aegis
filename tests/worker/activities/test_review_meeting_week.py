"""gather_meeting_week — SQL over meeting_review rows and meeting observations."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from aegis.services.observations import record_external_observation
from aegis_worker.activities.review import ReviewActivities

pytestmark = pytest.mark.asyncio
PREFIX = "mw-test-"


async def _content(pool, source_type, title, metadata, age_days=1):
    cid = PREFIX + uuid.uuid4().hex[:12]
    await pool.execute(
        "INSERT INTO knowledge_content (content_id, url, title, source_type, tags, metadata, ingested_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, now() - make_interval(days => $7))",
        cid, f"test://{cid}", title, source_type, [source_type], metadata, age_days,
    )


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    async def _clean():
        await db_pool.execute("DELETE FROM knowledge_content WHERE content_id LIKE $1", PREFIX + "%")
        await db_pool.execute("DELETE FROM life.observations WHERE source='meeting' AND external_id LIKE $1", PREFIX + "%")
    await _clean()
    yield db_pool
    await _clean()


async def test_empty_week_returns_empty_shape(pool):
    out = await ReviewActivities(db_pool=pool).gather_meeting_week()
    assert out == {
        "meetings": [], "talk_share_avg": None, "talk_share_prev": None,
        "words_per_turn_avg": None, "words_per_turn_prev": None, "missing_doc_by_account": {},
    }


async def test_gathers_reviews_averages_and_missing_docs(pool):
    review = {"contributions": ["c1"], "problems_raised": ["p1"], "commitments": ["k1"], "verbosity_note": "v1"}
    await _content(pool, "meeting_review", "Standup", {"review": review, "stats": {"self": {"talk_share_pct": 12.0}}})
    await _content(pool, "meeting_review", "Old one", {"review": review, "stats": {}}, age_days=9)
    await _content(pool, "meeting", "Standup", {"doc_status": "ok", "account": "acct-a"})
    await _content(pool, "meeting", "Forwarded", {"doc_status": "no_drive_scope", "account": "acct-b"})
    await _content(pool, "meeting", "Forwarded 2", {"doc_status": "inaccessible", "account": "acct-b"})
    now = datetime.now(UTC)
    for days, share, wpt in ((1, 10.0, 30.0), (2, 14.0, 46.0), (9, 20.0, 60.0)):
        ext = f"{PREFIX}{days}"
        await record_external_observation(pool, "meeting", "talk_share_pct", ext, share, now - timedelta(days=days))
        await record_external_observation(pool, "meeting", "words_per_turn", ext, wpt, now - timedelta(days=days))

    out = await ReviewActivities(db_pool=pool).gather_meeting_week()
    assert [m["title"] for m in out["meetings"]] == ["Standup"]
    m = out["meetings"][0]
    assert m["talk_share_pct"] == 12.0 and m["contributions"] == ["c1"]
    assert m["problems_raised"] == ["p1"] and m["commitments"] == ["k1"] and m["verbosity_note"] == "v1"
    assert out["talk_share_avg"] == 12.0 and out["talk_share_prev"] == 20.0
    assert out["words_per_turn_avg"] == 38.0 and out["words_per_turn_prev"] == 60.0
    assert out["missing_doc_by_account"] == {"acct-b": 2}
