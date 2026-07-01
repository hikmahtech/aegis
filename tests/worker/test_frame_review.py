"""frame_review — decision building, LLM ranking, deterministic fallback."""
from __future__ import annotations

import pytest
from aegis_worker.activities.review import ReviewActivities

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
        async def think(self, prompt, model=None):
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
        async def think(self, prompt, model=None):
            raise RuntimeError("proxy timeout")
    acts = ReviewActivities(db_pool=None, llm_client=_LLM())
    out = await acts.frame_review(SNAP)
    assert "Weekly review" in out["narrative"]
    assert len(out["decisions"]) == 3
