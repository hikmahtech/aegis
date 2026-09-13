"""The daylog under the user's clock and the vault layout (the OSS audit).

The day is bounded in `user_timezone`, the deterministic entry's wording and
the rollup's week rule come from the `vault_layout` row, and a run with no
agent resolves the `research` holder instead of naming one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from aegis.services import vault_layout as vl
from aegis_worker.activities.daylog import (
    DayLogActivities,
    _day_bounds,
    _format_daylog_fallback,
    _format_rollup_fallback,
    _language_ask,
)
from aegis_worker.flows.daylog import DayLogConfig, rollup_window
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment

from tests.worker.test_daylog import (
    _add_completed_task,
    _RecordingKS,
    _run_flow,
    _wipe,
)


@pytest_asyncio.fixture(loop_scope="function")
async def clean_db(db_pool):
    async with db_pool.acquire() as conn:
        await _wipe(conn)
        await conn.execute("DELETE FROM settings WHERE key IN ('user_timezone', 'vault_layout')")
    vl.invalidate_cache()
    yield db_pool
    async with db_pool.acquire() as conn:
        await _wipe(conn)
        await conn.execute("DELETE FROM settings WHERE key IN ('user_timezone', 'vault_layout')")
    vl.invalidate_cache()


async def _set(pool, key: str, value) -> None:
    await pool.execute(
        "INSERT INTO settings (key, value) VALUES ($1, $2) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        key,
        value,
    )
    vl.invalidate_cache()


# ------------------------------------------------------------- timezone


def test_day_bounds_are_on_the_given_clock():
    start, end = _day_bounds("2026-09-12")
    assert (start, end) == (
        datetime(2026, 9, 12, tzinfo=UTC), datetime(2026, 9, 13, tzinfo=UTC)
    )
    kolkata = ZoneInfo("Asia/Kolkata")
    start, end = _day_bounds("2026-09-12", kolkata)
    assert start == datetime(2026, 9, 12, tzinfo=kolkata)
    assert start.astimezone(UTC) == datetime(2026, 9, 11, 18, 30, tzinfo=UTC)
    assert end.astimezone(UTC) == datetime(2026, 9, 12, 18, 30, tzinfo=UTC)


@pytest.mark.asyncio
async def test_gather_day_events_bounds_the_day_in_the_users_timezone(clean_db):
    """A task completed at 22:00 UTC on the 11th is the 12th in Kiritimati
    (UTC+14) — and the 11th on the container's clock. This failed before the
    bounds moved off UTC."""
    await _add_completed_task(clean_db, "Late in UTC, early there", datetime(2026, 9, 11, 22, 0, tzinfo=UTC))
    acts = DayLogActivities(db_pool=clean_db)
    env = ActivityEnvironment()
    before = await env.run(acts.gather_day_events, "2026-09-12")
    assert before["counts"]["tasks"] == 0
    await _set(clean_db, "user_timezone", "Pacific/Kiritimati")
    after = await env.run(acts.gather_day_events, "2026-09-12")
    assert [t["content"] for t in after["tasks"]] == ["Late in UTC, early there"]
    assert (await env.run(acts.gather_day_events, "2026-09-11"))["counts"]["tasks"] == 0


# -------------------------------------------------------------- wording


def test_the_fallback_wording_comes_from_the_language_table():
    events = {"tasks": [{"content": "Ship it"}], "meetings": [], "decisions": []}
    assert _format_daylog_fallback(events, "2026-09-12") == "Day log for 2026-09-12.\nCompleted:\n  - Ship it"
    assert _format_daylog_fallback({}, "2026-09-12") == (
        "Day log for 2026-09-12. Quiet day — nothing was recorded."
    )
    words = {"daylog_title": "Tagebuch {date}.", "tasks": "Erledigt:", "quiet_day": "Ruhiger Tag."}
    assert _format_daylog_fallback(events, "2026-09-12", words) == "Tagebuch 2026-09-12.\nErledigt:\n  - Ship it"
    assert _format_daylog_fallback({}, "2026-09-12", words) == "Tagebuch 2026-09-12. Ruhiger Tag."
    assert _format_rollup_fallback([], "weekly", "2026-W37") == "Weekly log 2026-W37 — 0 day(s) recorded."
    assert _format_rollup_fallback(
        [], "weekly", "2026-W37", {"rollup_header": "{period} {label}: {n} Tage."}
    ) == "Weekly 2026-W37: 0 Tage."


def test_english_adds_nothing_to_the_prompts_and_another_language_asks():
    assert _language_ask(vl.DEFAULT_LAYOUT) == ""
    assert _language_ask(None) == ""
    assert _language_ask(vl.layout_from({"language": {"name": "German"}})) == " Write in German."


@pytest.mark.asyncio
async def test_distil_without_an_llm_uses_the_rows_wording(clean_db):
    await _set(clean_db, "vault_layout", {"language": {"daylog_title": "Log {date}:"}})
    acts = DayLogActivities(db_pool=clean_db, llm_client=None)
    out = await ActivityEnvironment().run(
        acts.distil_daylog, {"tasks": [{"content": "x"}]}, "2026-09-12", ""
    )
    assert out.startswith("Log 2026-09-12:\n")


# ----------------------------------------------------------------- weeks


def test_rollup_window_follows_the_week_rule():
    sat = datetime(2026, 9, 12, 20, 20, tzinfo=UTC)
    assert rollup_window("weekly", sat) == ("2026-09-07", "2026-09-13", "2026-W37")
    assert rollup_window("weekly", sat, "sunday", "locale_us") == ("2026-09-06", "2026-09-12", "2026-W37")
    sun = datetime(2026, 9, 13, 20, 20, tzinfo=UTC)
    assert rollup_window("weekly", sun, "sunday", "locale_us") == ("2026-09-13", "2026-09-19", "2026-W38")


@pytest.mark.asyncio
async def test_vault_week_rule_reads_the_row(clean_db):
    acts = DayLogActivities(db_pool=clean_db)
    assert await ActivityEnvironment().run(acts.vault_week_rule) == {
        "week_start": "monday", "week_numbering": "iso",
    }
    await _set(clean_db, "vault_layout", {"week_start": "sunday", "week_numbering": "locale_us"})
    assert await ActivityEnvironment().run(acts.vault_week_rule) == {
        "week_start": "sunday", "week_numbering": "locale_us",
    }


# ---------------------------------------------------------------- owner


@pytest.mark.asyncio
async def test_a_run_with_no_agent_is_owned_by_the_research_holder(clean_db):
    """`DayLogConfig()` names nobody; the flow resolves the `research`
    capability's holder and the LLM ledger / journal entry carry that id."""
    from aegis_worker.activities.agent_registry import AgentRegistryActivities
    from temporalio import activity

    holder = await clean_db.fetchval(
        "SELECT id FROM agents WHERE active AND capabilities @> '[\"research\"]'::jsonb "
        "ORDER BY id LIMIT 1"
    )
    assert holder
    seen: list = []

    @activity.defn(name="notes_journal_write")
    async def journal(entry: dict) -> dict:
        seen.append(entry)
        return {"status": "written", "path": "x.md"}

    ks = _RecordingKS()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        result = await _run_flow(
            env.client,
            ks,
            DayLogActivities(db_pool=clean_db, llm_client=None),
            "daylog-owner-1",
            extra_activities=[journal, AgentRegistryActivities(db_pool=clean_db).resolve_agents],
            config=DayLogConfig(),
        )
    assert result["status"] == "journaled"
    assert seen[0]["agent_id"] == holder
