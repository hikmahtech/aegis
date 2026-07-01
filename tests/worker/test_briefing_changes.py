"""gather_briefing_changes — diff against prior briefing_state."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aegis.db import run_migrations
from aegis_worker.activities.briefing import BriefingActivities


def _iso(dt):
    return dt.replace(tzinfo=UTC).isoformat()


@pytest_asyncio.fixture(loop_scope="function")
async def _seeded(db_pool):
    await run_migrations(db_pool)
    cursor = datetime.now(UTC) - timedelta(hours=12)
    async with db_pool.acquire() as conn:
        # prior state: cursor 12h ago, 2 contradictions seen, no seen ids
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ('briefing_state', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            {"last_briefing_at": _iso(cursor), "contradiction_count": 2,
             "seen_intel_ids": [], "seen_calendar_ids": []},
        )
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ('calendar_events_test', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            [{"id": "evt-new", "summary": "New Standup", "start": "2026-06-23T09:00:00Z"}],
        )
        # a failed run AFTER the cursor (must surface) + one before (must not)
        await conn.execute("DELETE FROM workflow_runs WHERE run_id LIKE 'brchg-%'")
        await conn.execute(
            "INSERT INTO workflow_runs (run_id, workflow_id, workflow_type, status, "
            "started_at, completed_at, error) VALUES "
            "('brchg-fail','wf1','RaindropIngestFlow','failed', now()-interval '2 hours', "
            " now()-interval '2 hours', 'boom'), "
            "('brchg-old','wf2','RssIngestFlow','failed', now()-interval '2 days', "
            " now()-interval '2 days', 'old')"
        )
        # an open drift after the cursor
        await conn.execute(
            "DELETE FROM pandoras_actor.homelab_drift WHERE service_name LIKE 'brchg-%'"
        )
        await conn.execute(
            "INSERT INTO pandoras_actor.homelab_drift "
            "(service_name, stack_name, drift_type, expected, actual, severity, alert_key, detected_at) "
            "VALUES ('brchg-svc', 'test', 'config', '{}'::jsonb, '{}'::jsonb, 'warning', 'brchg-svc-key', now()-interval '1 hour')"
        )
    return db_pool


def _kc(intel=None, contradictions=3):
    kc = AsyncMock()
    kc.search = AsyncMock(return_value=intel or [])
    kc.get_stats = AsyncMock(return_value={"triples": 1, "entities": 1, "content": 1})
    kc.contradictions = AsyncMock(
        return_value=[{"a": i} for i in range(contradictions)]
    )
    return kc


@pytest.mark.asyncio
async def test_changes_fire_on_planted(db_pool, _seeded):
    kc = _kc(intel=[
        {"content_id": "i1", "title": "GPT-6 ships",
         "metadata": {"significance": 5, "topic": "ai"},
         "ingested_at": _iso(datetime.now(UTC))},
        {"content_id": "i2", "title": "minor",
         "metadata": {"significance": 2}, "ingested_at": _iso(datetime.now(UTC))},
    ], contradictions=3)
    act = BriefingActivities(db_pool=db_pool, knowledge_connector=kc)
    out = await act.gather_briefing_changes()
    assert out["quiet"] is False
    assert [i["title"] for i in out["intel"]] == ["GPT-6 ships"]  # sig>=4 only
    assert any(r["workflow_type"] == "RaindropIngestFlow" for r in out["broke"]["failed_runs"])
    assert not any(r["workflow_type"] == "RssIngestFlow" for r in out["broke"]["failed_runs"])
    assert any(d["service"] == "brchg-svc" for d in out["broke"]["new_drift"])
    assert "evt-new" in out["calendar"]["new_ids"]
    assert "i1" in out["_new_state"]["seen_intel_ids"]


@pytest.mark.asyncio
async def test_changes_quiet_on_clean(db_pool):
    await run_migrations(db_pool)
    async with db_pool.acquire() as conn:
        # cursor in the FUTURE so nothing is "after" it; contradictions match prior
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ('briefing_state', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            {"last_briefing_at": _iso(datetime.now(UTC) + timedelta(hours=1)),
             "contradiction_count": 3, "seen_intel_ids": [], "seen_calendar_ids": []},
        )
        await conn.execute("DELETE FROM settings WHERE key LIKE 'calendar_events_%'")
    act = BriefingActivities(db_pool=db_pool, knowledge_connector=_kc(intel=[], contradictions=3))
    out = await act.gather_briefing_changes()
    assert out["quiet"] is True
    assert out["intel"] == []
    assert out["broke"]["failed_runs"] == []
