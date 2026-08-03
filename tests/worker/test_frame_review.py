"""frame_review — decision building, LLM ranking, deterministic fallback."""
from __future__ import annotations

import pytest
from aegis_worker.activities.review import ReviewActivities

from tests.llm_stub import StubbedLLMClient

SNAP = {
    "stale_next_actions_count": 0, "stale_next_actions_top3": [],
    "someday_count": 1, "waiting_stale_7d_count": 1, "waiting_stale_top": [],
    "inbox_unclarified_7d_count": 0, "completed_7d_count": 3,
    "never_clarified_count": 0, "never_clarified_oldest5": [],
    "stalled_projects": [{"project_id": "P1", "name": "Site", "url": "u"}],
    "aging_waiting_items": [{"task_id": "T_W", "content": "chase X", "days": 9, "url": "u"}],
    "slipping_items": [{"task_id": "T_S", "content": "file taxes", "due_date": "2026-06-01", "url": "u"}],
    "to_read_count": 4,
    "someday_resurface_items": [{"task_id": "T_SM", "content": "violin", "age_days": 120}],
    "_top_n": 5,
}


def test_build_decisions_only_card_signals():
    acts = ReviewActivities(db_pool=None)
    decs = acts._build_decisions(SNAP)
    signals = {d["signal"] for d in decs}
    # Card signals present; stalled/to_read are digest-only (no decision).
    assert signals == {"aging_waiting", "slipping", "someday_resurface"}
    waiting = next(d for d in decs if d["signal"] == "aging_waiting")
    assert waiting["task_id"] == "T_W"
    assert set(waiting["options"]) == {"nudge", "done", "drop", "keep"}


@pytest.mark.asyncio
async def test_frame_review_fallback_when_no_llm():
    acts = ReviewActivities(db_pool=None, llm_client=None)
    out = await acts.frame_review(SNAP)
    assert "Weekly review" in out["narrative"]  # format_weekly_preview output
    assert 0 < len(out["decisions"]) <= 5


@pytest.mark.asyncio
async def test_frame_review_uses_llm_order_and_narrative():
    class _LLM:
        async def think(self, prompt, model=None, **kwargs):
            return {"response": '{"narrative":"Focus week.","order":["slipping:T_S"]}'}
    acts = ReviewActivities(db_pool=None, llm_client=_LLM())
    out = await acts.frame_review(SNAP)
    assert out["narrative"] == "Focus week."
    # LLM put slipping first; the rest are appended, none dropped.
    assert out["decisions"][0]["id"] == "slipping:T_S"
    assert len(out["decisions"]) == 3


@pytest.mark.asyncio
async def test_frame_review_fallback_on_llm_error():
    class _LLM:
        async def think(self, prompt, model=None, **kwargs):
            raise RuntimeError("proxy timeout")
    acts = ReviewActivities(db_pool=None, llm_client=_LLM())
    out = await acts.frame_review(SNAP)
    assert "Weekly review" in out["narrative"]
    assert len(out["decisions"]) == 3


@pytest.mark.asyncio
async def test_frame_review_records_the_llm_call(db_pool):
    """issue #106: the weekly review's narrative/ranking call wrote NO
    llm_calls row — not even on failure — because it passed neither `db_pool`
    nor `purpose`.

    Real `LLMClient` (only the HTTP layer stubbed) against the real pool, row
    read back: `record_llm_call` swallows its own errors, so a mock assertion
    passes against a write that never landed.
    """
    await db_pool.execute("DELETE FROM llm_calls WHERE purpose = 'review_frame'")
    llm = StubbedLLMClient(
        db_pool=db_pool, content='{"narrative":"Focus week.","order":["slipping:T_S"]}'
    )
    acts = ReviewActivities(db_pool=db_pool, llm_client=llm)
    try:
        out = await acts.frame_review(SNAP)
        assert out["narrative"] == "Focus week."

        rows = await db_pool.fetch(
            "SELECT status, agent_id, output_tokens FROM llm_calls "
            "WHERE purpose = 'review_frame'"
        )
        assert len(rows) == 1, f"expected one review_frame row, got {len(rows)}"
        assert rows[0]["status"] == "success"
        assert rows[0]["agent_id"] == "sebas"
        assert rows[0]["output_tokens"] == 22
    finally:
        await db_pool.execute("DELETE FROM llm_calls WHERE purpose = 'review_frame'")


@pytest.mark.asyncio
async def test_frame_review_records_a_failed_llm_call(db_pool):
    """The deterministic fallback must not hide the failure too."""
    await db_pool.execute("DELETE FROM llm_calls WHERE purpose = 'review_frame'")
    llm = StubbedLLMClient(db_pool=db_pool, raises=RuntimeError("proxy timeout"))
    acts = ReviewActivities(db_pool=db_pool, llm_client=llm)
    try:
        out = await acts.frame_review(SNAP)
        assert "Weekly review" in out["narrative"]

        rows = await db_pool.fetch(
            "SELECT status, error FROM llm_calls WHERE purpose = 'review_frame'"
        )
        assert len(rows) == 1, f"expected one review_frame row, got {len(rows)}"
        assert rows[0]["status"] == "timeout"
        assert "proxy timeout" in (rows[0]["error"] or "")
    finally:
        await db_pool.execute("DELETE FROM llm_calls WHERE purpose = 'review_frame'")
