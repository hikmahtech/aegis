"""gather_briefing_changes — diff against prior briefing_state."""
from __future__ import annotations

import json
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
async def test_changes_includes_completed_run_with_error_result_summary(db_pool, _seeded):
    """A flow can return `{"status": "error", "reason": "..."}` without
    raising — the workflow_run interceptor then records status='completed'
    with error IS NULL, so the plain `status='failed' OR error IS NOT NULL`
    predicate misses it entirely. gather_briefing_changes must also catch
    result_summary->>'status' = 'error' so this class of silent failure
    still surfaces in the digest."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO workflow_runs (run_id, workflow_id, workflow_type, status, "
            "started_at, completed_at, result_summary) VALUES "
            "($1, $2, $3, 'completed', now()-interval '2 hours', now()-interval '2 hours', $4)",
            "brchg-compl-err",
            "wf3",
            "AgentChatReplyFlow",
            {"status": "error", "reason": "Connection error.", "message_id": None},
        )
    act = BriefingActivities(db_pool=db_pool, knowledge_connector=_kc(intel=[], contradictions=3))
    out = await act.gather_briefing_changes()
    types = {r["workflow_type"] for r in out["broke"]["failed_runs"]}
    # both the genuinely-failed run (from _seeded) and the completed-but-error
    # run must be present
    assert "RaindropIngestFlow" in types
    assert "AgentChatReplyFlow" in types
    compl_err = next(r for r in out["broke"]["failed_runs"] if r["workflow_type"] == "AgentChatReplyFlow")
    assert "Connection error." in compl_err["error"]


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


# ---------------------------------------------------------------------------
# B5 — the `current_place` dimension.
#
# The KV holds {"place": <label>, "at": <iso>}, written by the location
# webhook. Every assertion below uses a literal age rather than the production
# staleness constant, so moving that constant is a visible test change and not
# a silently self-adjusting one.
# ---------------------------------------------------------------------------


async def _set_place(pool, value) -> None:
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM settings WHERE key = 'current_place'")
        if value is not None:
            await conn.execute(
                "INSERT INTO settings (key, value) VALUES ('current_place', $1)", value
            )


@pytest.mark.asyncio
async def test_fresh_current_place_surfaces_in_the_briefing(db_pool, _seeded):
    await _set_place(
        db_pool,
        {"place": "zzb5b-office", "at": _iso(datetime.now(UTC) - timedelta(hours=1))},
    )
    act = BriefingActivities(db_pool=db_pool, knowledge_connector=_kc())
    out = await act.gather_briefing_changes()
    assert out["place"]["place"] == "zzb5b-office"
    assert "zzb5b-office" in act._format_changes_fallback(out)


@pytest.mark.asyncio
async def test_stale_current_place_degrades_to_no_place_line(db_pool, _seeded):
    """A pointer from two days ago is worse than none — the phone has been off
    or the push has broken, and the briefing must not assert where you are."""
    await _set_place(
        db_pool,
        {"place": "zzb5b-office", "at": _iso(datetime.now(UTC) - timedelta(hours=48))},
    )
    act = BriefingActivities(db_pool=db_pool, knowledge_connector=_kc())
    out = await act.gather_briefing_changes()
    assert out["place"] == {}
    assert "zzb5b-office" not in act._format_changes_fallback(out)


@pytest.mark.asyncio
async def test_absent_current_place_is_no_place_line_not_an_exception(db_pool, _seeded):
    await _set_place(db_pool, None)
    act = BriefingActivities(db_pool=db_pool, knowledge_connector=_kc())
    out = await act.gather_briefing_changes()
    assert out["place"] == {}
    # the rest of the briefing still gathered normally
    assert any(
        r["workflow_type"] == "RaindropIngestFlow" for r in out["broke"]["failed_runs"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        {"place": "zzb5b-office"},  # no timestamp
        {"place": "zzb5b-office", "at": "not-a-date"},
        {"at": "2026-07-30T00:00:00+00:00"},  # no label
        ["not", "an", "object"],
    ],
)
async def test_garbage_current_place_never_kills_the_briefing(db_pool, _seeded, value):
    await _set_place(db_pool, value)
    act = BriefingActivities(db_pool=db_pool, knowledge_connector=_kc())
    out = await act.gather_briefing_changes()
    assert out["place"] == {}
    assert any(
        r["workflow_type"] == "RaindropIngestFlow" for r in out["broke"]["failed_runs"]
    )


# ---------------------------------------------------------------------------
# Health dimension (B6) — the newest reading of each metric the health push
# writes. As above, every assertion uses a literal age, not the production
# staleness constant. Rows are scoped by `source='health'`, which only the B6
# lane ever writes.
# ---------------------------------------------------------------------------


async def _set_health(pool, metric, value, age_hours) -> None:
    """One health observation `age_hours` old, shaped the way ingest writes it."""
    observed_at = datetime.now(UTC) - timedelta(hours=age_hours)
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM life.observations WHERE source = 'health'")
        await conn.execute(
            "INSERT INTO life.observations (source, metric, value, observed_at, external_id) "
            "VALUES ('health', $1, $2, $3, $4)",
            metric,
            value,
            observed_at,
            f"zzb6b-{observed_at.isoformat()}",
        )


@pytest.mark.asyncio
async def test_a_fresh_health_reading_surfaces_in_the_briefing(db_pool, _seeded):
    await _set_health(db_pool, "resting_hr", 54, age_hours=6)
    act = BriefingActivities(db_pool=db_pool, knowledge_connector=_kc())
    assert await act._recent_health() == {"resting_hr": 54.0}
    framed = await act.frame_briefing(await act.gather_briefing_changes())
    assert "<b>Health</b>: resting_hr 54.0" in framed


@pytest.mark.asyncio
async def test_no_health_value_crosses_the_workflow_boundary(db_pool, _seeded):
    """`gather_briefing_changes`'s return value is `DailyBriefingFlow`'s
    `changes`, which Temporal persists verbatim as an activity result and again
    as `frame_briefing`'s argument — a second store for the owner's body data,
    with its own retention and its own web UI, which is exactly what
    `aegis.services.health` refused to create when it made ingest inline.

    So the reading must not be anywhere in that bundle, at any depth.
    """
    await _set_health(db_pool, "resting_hr", 54321.5, age_hours=6)
    act = BriefingActivities(db_pool=db_pool, knowledge_connector=_kc())

    # Non-vacuity: the reading really is there to be leaked. Without this the
    # test would pass just as happily against an empty life.observations.
    assert await act._recent_health() == {"resting_hr": 54321.5}

    bundle = await act.gather_briefing_changes()  # the activity RESULT ...
    blob = json.dumps(bundle, default=str)  # ... and frame_briefing's ARGUMENT
    assert "resting_hr" not in blob
    assert "54321" not in blob  # distinctive enough not to collide with a timestamp
    # Non-vacuity: the bundle really does carry everything else.
    assert "RaindropIngestFlow" in blob


@pytest.mark.asyncio
async def test_the_framing_prompt_never_carries_a_health_value(db_pool, _seeded):
    """The framing model is `model_balanced`, which may be a hosted API. The
    health line is rendered deterministically so body data never goes to it.

    Health is injected into the bundle by hand here on purpose: since
    `gather_briefing_changes` stopped emitting it, feeding this the real bundle
    would prove nothing about `_build_briefing_prompt`'s own filter — the two
    guards would mask each other. This proves the prompt-side one alone.
    """
    act = BriefingActivities(db_pool=db_pool, knowledge_connector=_kc())
    bundle = {**await act.gather_briefing_changes(), "health": {"resting_hr": 54321.5}}
    prompt = act._build_briefing_prompt(bundle)
    assert "resting_hr" not in prompt
    assert "54321" not in prompt  # distinctive enough not to collide with a timestamp
    # Non-vacuity: the prompt really does carry the rest of the bundle.
    assert "RaindropIngestFlow" in prompt


@pytest.mark.asyncio
async def test_the_health_block_survives_an_llm_narrative(db_pool, _seeded):
    """It is appended after the narrative, so a chatty model cannot drop it."""
    await _set_health(db_pool, "steps", 8421, age_hours=6)
    llm = AsyncMock()
    llm.think = AsyncMock(return_value={"response": "Nothing much happened."})
    act = BriefingActivities(db_pool=db_pool, knowledge_connector=_kc(), llm_client=llm)
    out = await act.gather_briefing_changes()
    framed = await act.frame_briefing(out)
    assert framed.startswith("Nothing much happened.")
    assert "<b>Health</b>: steps 8421.0" in framed


@pytest.mark.asyncio
async def test_a_health_reading_from_last_week_is_not_reported_as_today(db_pool, _seeded):
    await _set_health(db_pool, "resting_hr", 54, age_hours=24 * 7)
    act = BriefingActivities(db_pool=db_pool, knowledge_connector=_kc())
    assert await act._recent_health() == {}
    framed = await act.frame_briefing(await act.gather_briefing_changes())
    assert "<b>Health</b>" not in framed


@pytest.mark.asyncio
async def test_only_the_newest_reading_of_each_metric_is_reported(db_pool, _seeded):
    """A push writes one row per sample, so the window holds several of each."""
    await _set_health(db_pool, "steps", 1, age_hours=30)  # clears, then seeds
    async with db_pool.acquire() as conn:
        for metric, value, age in (
            ("steps", 8421, 4),
            ("resting_hr", 61, 20),
            ("resting_hr", 54, 5),
        ):
            observed_at = datetime.now(UTC) - timedelta(hours=age)
            await conn.execute(
                "INSERT INTO life.observations "
                "(source, metric, value, observed_at, external_id) "
                "VALUES ('health', $1, $2, $3, $4)",
                metric,
                value,
                observed_at,
                f"zzb6b-{metric}-{observed_at.isoformat()}",
            )
    act = BriefingActivities(db_pool=db_pool, knowledge_connector=_kc())
    assert await act._recent_health() == {"steps": 8421.0, "resting_hr": 54.0}
