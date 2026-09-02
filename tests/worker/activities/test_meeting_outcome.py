"""record_analysis_outcome — stamp the analysis verdict onto the stored row.

The `meeting` row is filed before the analysis runs, so the outcome is written
back with a targeted metadata update. Never a re-ingest: that would re-embed
18k characters per meeting for one string.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from aegis_worker.activities.meeting import MeetingActivities

pytestmark = pytest.mark.asyncio
PREFIX = "mo-test-"


class _BrokenPool:
    """A pool that is up as far as the caller can see and fails on use —
    what a closed pool looks like from inside the activity."""

    def acquire(self):
        raise RuntimeError("pool is closed")


@pytest_asyncio.fixture(loop_scope="function")
async def pool(db_pool):
    async def _clean():
        await db_pool.execute(
            "DELETE FROM knowledge_content WHERE content_id LIKE $1", PREFIX + "%"
        )

    await _clean()
    yield db_pool
    await _clean()


async def _insert(pool, metadata: dict) -> str:
    cid = PREFIX + uuid.uuid4().hex[:12]
    await pool.execute(
        "INSERT INTO knowledge_content (content_id, url, title, source_type, tags, metadata) "
        "VALUES ($1, $2, 'Standup', 'meeting', '{meeting}', $3)",
        cid,
        f"test://{cid}",
        metadata,
    )
    return cid


async def _analysis(pool, cid: str):
    return await pool.fetchval(
        "SELECT metadata->>'analysis' FROM knowledge_content WHERE content_id = $1", cid
    )


async def test_the_outcome_lands_and_the_existing_metadata_survives(pool):
    cid = await _insert(pool, {"doc_status": "ok", "account": "acct-a", "speakers": ["Sam"]})

    assert await MeetingActivities("c", "t", db_pool=pool).record_analysis_outcome(
        cid, "self_not_matched"
    ) == {"recorded": True}

    row = await pool.fetchrow(
        "SELECT metadata FROM knowledge_content WHERE content_id = $1", cid
    )
    md = row["metadata"]
    if isinstance(md, str):  # no jsonb codec registered on this pool
        import json

        md = json.loads(md)
    assert md["analysis"] == "self_not_matched"
    assert md["doc_status"] == "ok" and md["account"] == "acct-a"
    assert md["speakers"] == ["Sam"]


async def test_a_rerun_that_succeeds_clears_the_stale_skip_reason(pool):
    """The flow writes the outcome on BOTH paths for exactly this reason."""
    cid = await _insert(pool, {"analysis": "self_not_matched"})
    acts = MeetingActivities("c", "t", db_pool=pool)

    assert await acts.record_analysis_outcome(cid, "ok") == {"recorded": True}
    assert await _analysis(pool, cid) == "ok"


async def test_an_unknown_content_id_is_reported_not_raised(pool):
    acts = MeetingActivities("c", "t", db_pool=pool)
    assert await acts.record_analysis_outcome(PREFIX + "nope", "ok") == {"recorded": False}


async def test_a_blank_id_a_missing_pool_and_a_broken_pool_never_raise(pool):
    """Best-effort: this runs fire-and-forget off the end of MeetingNotesFlow,
    so every failure mode is a warning and a False, never an exception."""
    assert await MeetingActivities("c", "t", db_pool=pool).record_analysis_outcome(
        "", "ok"
    ) == {"recorded": False}
    assert await MeetingActivities("c", "t", db_pool=None).record_analysis_outcome(
        "cid", "ok"
    ) == {"recorded": False}
    assert await MeetingActivities(
        "c", "t", db_pool=_BrokenPool()
    ).record_analysis_outcome("cid", "ok") == {"recorded": False}
