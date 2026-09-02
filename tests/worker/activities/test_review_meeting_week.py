"""gather_meeting_week — SQL over meeting_review rows and meeting observations."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from aegis.services.observations import record_external_observation
from aegis_worker.activities.review import ReviewActivities, format_meeting_week

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
        "no_review_by_reason": {},
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
    assert out["missing_doc_by_account"] == {"acct-b": {"no_drive_scope": 1, "inaccessible": 1}}


async def test_the_week_is_picked_by_meeting_date_not_ingest_time(pool):
    """A backfill re-ingests old meetings, so `ingested_at` called every one of
    them "this week" — the first backfill listed 20. Only the meeting's own date
    makes the list right, and a malformed one must fall back, not raise."""
    review = {"contributions": [], "problems_raised": [], "commitments": [], "verbosity_note": ""}
    now = datetime.now(UTC)

    def _held(days_ago):
        return {"review": review, "stats": {}, "meeting_date": (now - timedelta(days=days_ago)).isoformat()}

    # Backfilled today, held three weeks ago ⇒ out of the week.
    await _content(pool, "meeting_review", "Backfilled old", _held(20), age_days=0)
    # Filed ten days late, held two days ago ⇒ in the week.
    await _content(pool, "meeting_review", "Late filed", _held(2), age_days=10)
    # An unparseable meeting_date falls back to ingest time inside SQL.
    await _content(
        pool, "meeting_review", "Garbled date",
        {"review": review, "stats": {}, "meeting_date": "garbage"}, age_days=0,
    )
    # The same expression governs the missing-doc counts over `meeting` rows.
    await _content(
        pool, "meeting", "Backfilled no doc",
        {"doc_status": "no_drive_scope", "account": "acct-x",
         "meeting_date": (now - timedelta(days=20)).isoformat()}, age_days=0,
    )

    out = await ReviewActivities(db_pool=pool).gather_meeting_week()
    titles = [m["title"] for m in out["meetings"]]
    assert "Backfilled old" not in titles
    assert "Late filed" in titles
    assert "Garbled date" in titles
    assert "acct-x" not in out["missing_doc_by_account"]


async def test_the_block_shows_the_meeting_title_not_the_review_prefix(pool):
    """The review row's own title is `Meeting review: <title>`, and Gemini appends
    " – Notes by Gemini" to every doc name, so the block read
    "• Meeting review: Data Foundations: Session 4 - S…" with the real title
    clipped away. Display only — the stored title is untouched."""
    await _content(
        pool, "meeting_review", "Meeting review: Standup – 2026/09/01 09:30 BST – Notes by Gemini",
        {"title": "Standup – 2026/09/01 09:30 BST – Notes by Gemini",
         "review": {"contributions": [], "problems_raised": [], "commitments": [], "verbosity_note": ""},
         "stats": {"self": {"talk_share_pct": 9.0}}},
    )
    # No metadata.title (a pre-fix row): fall back to the row title, prefix and
    # hyphen-spelled Gemini suffix stripped.
    await _content(pool, "meeting_review", "Meeting review: Retro - Notes by Gemini", {"review": {}, "stats": {}})

    out = await ReviewActivities(db_pool=pool).gather_meeting_week()
    assert {m["title"] for m in out["meetings"]} == {"Standup – 2026/09/01 09:30 BST", "Retro"}
    assert "• Standup – 2026/09/01 09:30 BST — you spoke 9%" in format_meeting_week(out)


async def test_meetings_filed_without_a_review_are_counted_by_meeting_date(pool):
    """A skipped analysis was visible only in workflow_runs — 16 of the first 63
    backfilled meetings were in that state. Counted in the SAME meeting-date
    window as the meetings list, so a backfill cannot make an old one "this
    week"; `analysis: ok` and a row not yet stamped are not counted at all."""
    now = datetime.now(UTC)

    def _meeting(days_ago, **extra):
        return {
            "doc_status": "ok",
            "account": "acct-a",
            "meeting_date": (now - timedelta(days=days_ago)).isoformat(),
            **extra,
        }

    await _content(pool, "meeting", "Skipped", _meeting(2, analysis="self_not_matched"), age_days=0)
    await _content(pool, "meeting", "Skipped 2", _meeting(3, analysis="too_thin"), age_days=0)
    # Backfilled today, held three weeks ago ⇒ out of the window.
    await _content(pool, "meeting", "Old skip", _meeting(20, analysis="self_not_matched"), age_days=0)
    # Reviewed, and never stamped: neither is a warning.
    await _content(pool, "meeting", "Reviewed", _meeting(1, analysis="ok"), age_days=0)
    await _content(pool, "meeting", "Unstamped", _meeting(1), age_days=0)

    out = await ReviewActivities(db_pool=pool).gather_meeting_week()
    assert out["no_review_by_reason"] == {"self_not_matched": 1, "too_thin": 1}
    block = format_meeting_week(out)
    assert "⚠ 1 meeting filed without a review: no speaker matched your names" in block
    assert "⚠ 1 meeting filed without a review: too little text to review" in block
