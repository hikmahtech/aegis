"""frame_briefing — quiet line, deterministic fallback, LLM happy path."""
from __future__ import annotations

import pytest
from aegis_worker.activities.briefing import BriefingActivities

BUNDLE = {
    "quiet": False,
    "intel": [{"title": "GPT-6 ships", "significance": 5, "topic": "ai", "url": ""}],
    "broke": {"failed_runs": [{"workflow_type": "RaindropIngestFlow", "error": "boom"}],
              "new_drift": [{"service": "svc", "severity": "warning"}]},
    "knowledge": {"contradictions_delta": 1, "contradictions_total": 3, "top": []},
    "calendar": {"today": [{"summary": "Standup", "start": "2026-06-23T09:00:00Z"}],
                 "new_ids": ["evt-new"]},
    "_new_state": {},
}


@pytest.mark.asyncio
async def test_quiet_returns_one_liner():
    act = BriefingActivities()
    out = await act.frame_briefing({"quiet": True})
    assert "Quiet overnight" in out


@pytest.mark.asyncio
async def test_fallback_when_no_llm():
    act = BriefingActivities(llm_client=None)
    out = await act.frame_briefing(BUNDLE)
    assert "GPT-6 ships" in out
    assert "RaindropIngestFlow" in out


@pytest.mark.asyncio
async def test_llm_happy_path():
    class _LLM:
        async def think(self, prompt, model=None):
            return {"response": "Two things need you this morning."}
    act = BriefingActivities(llm_client=_LLM())
    out = await act.frame_briefing(BUNDLE)
    assert out == "Two things need you this morning."


@pytest.mark.asyncio
async def test_fallback_on_llm_error():
    class _LLM:
        async def think(self, prompt, model=None):
            raise RuntimeError("proxy down")
    act = BriefingActivities(llm_client=_LLM())
    out = await act.frame_briefing(BUNDLE)
    assert "GPT-6 ships" in out  # fell back to deterministic
