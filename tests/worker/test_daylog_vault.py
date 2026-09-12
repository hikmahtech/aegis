"""The daylog keeps the journal in the Obsidian vault (#514).

`test_daylog.py` runs the flow with the vault off (its old behaviour, which
must not change). These tests turn it on: a written or already-there journal
entry replaces the knowledge row, a vault error falls back to it, and a rollup
reads the days from the journal notes.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
import pytest_asyncio
from aegis.services import notes
from aegis_worker.activities.daylog import DayLogActivities
from aegis_worker.flows.daylog import DayLogConfig
from temporalio import activity
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment

from tests.notes_vault import CIPHER, device_commit, git, make_vault, needs_git
from tests.worker.test_daylog import (
    _add_completed_task,
    _add_daylog_entry,
    _iso_week_dates,
    _RecordingKS,
    _run_flow,
    _wipe,
)


@pytest_asyncio.fixture(loop_scope="function")
async def clean_db(db_pool):
    async with db_pool.acquire() as conn:
        await _wipe(conn)
    yield db_pool
    async with db_pool.acquire() as conn:
        await _wipe(conn)


def _journal_stub(result: dict, seen: list):
    @activity.defn(name="notes_journal_write")
    async def stub(entry: dict) -> dict:
        seen.append(entry)
        return result

    return stub


@pytest.mark.asyncio
async def test_a_journaled_day_files_no_knowledge_row(clean_db):
    ks = _RecordingKS()
    seen: list = []
    async with await WorkflowEnvironment.start_time_skipping() as env:
        with env.auto_time_skipping_disabled():
            now = await env.get_current_time()
            await _add_completed_task(clean_db, "Ship the pgvector migration", now)
            path = notes.daily_note_path(now.date())
            result = await _run_flow(
                env.client,
                ks,
                DayLogActivities(db_pool=clean_db, llm_client=None),
                "daylog-vault-1",
                extra_activities=[_journal_stub({"status": "written", "path": path}, seen)],
            )
    day = now.strftime("%Y-%m-%d")
    assert result["status"] == "journaled"
    assert result["path"] == path and result["vault"] == "written"
    assert ks.calls == [], "the vault holds the day, so no knowledge row may be filed"
    assert seen[0]["kind"] == "daily" and seen[0]["label"] == day and seen[0]["day"] == day
    assert "Ship the pgvector migration" in seen[0]["text"]
    state = await clean_db.fetchval("SELECT value FROM settings WHERE key = 'daylog_state'")
    assert state == {"last_date": day, "url": f"vault://{path}"}


@pytest.mark.asyncio
async def test_a_vault_error_falls_back_to_the_knowledge_row(clean_db):
    ks = _RecordingKS()
    seen: list = []
    async with await WorkflowEnvironment.start_time_skipping() as env:
        result = await _run_flow(
            env.client,
            ks,
            DayLogActivities(db_pool=clean_db, llm_client=None),
            "daylog-vault-2",
            extra_activities=[_journal_stub({"status": "error", "error": "push rejected"}, seen)],
        )
    assert result["status"] == "ingested"
    assert result["vault_error"] == "push rejected"
    assert len(ks.calls) == 1 and ks.calls[0]["source_type"] == "daylog"


@pytest.mark.asyncio
async def test_a_weekly_rollup_goes_to_the_journal(clean_db):
    ks = _RecordingKS()
    seen: list = []
    async with await WorkflowEnvironment.start_time_skipping() as env:
        with env.auto_time_skipping_disabled():
            now = await env.get_current_time()
            dates = _iso_week_dates(now)
            for i, d in enumerate(dates):
                await _add_daylog_entry(clean_db, d, [f"Day {i}: marker-{i}."])
            result = await _run_flow(
                env.client,
                ks,
                DayLogActivities(db_pool=clean_db, llm_client=None),
                "daylog-vault-3",
                extra_activities=[_journal_stub({"status": "exists", "path": "journal/W01 x.md"}, seen)],
                config=DayLogConfig(agent_id="raphael", mode="weekly"),
            )
    assert result["status"] == "journaled" and result["mode"] == "weekly"
    assert ks.calls == []
    assert seen[0]["kind"] == "weekly" and seen[0]["day"] == dates[0]
    assert "marker-3" in seen[0]["text"]


@needs_git
@pytest.mark.asyncio
async def test_a_rollup_reads_the_days_from_the_journal(clean_db, tmp_path):
    vault = make_vault(tmp_path)
    notes.write_sync(
        vault["cfg"],
        [
            notes.journal_append(
                "daily", date(2019, 3, 12), "2019-03-12", "From the vault journal.",
                datetime(2019, 3, 12, 21, 0),
            )
        ],
        "journal",
    )
    # The user wrote in the same note on the phone, including an encrypted block.
    git("pull", "-q", cwd=vault["device"])
    rel = notes.daily_note_path(date(2019, 3, 12))
    note = vault["device"] / rel
    device_commit(vault, {rel: note.read_text("utf-8") + f"\nmine {CIPHER}\n"})
    await _add_daylog_entry(clean_db, "2019-03-11", ["Knowledge row for the 11th."])
    await _add_daylog_entry(clean_db, "2019-03-12", ["Old knowledge row for the 12th."])

    acts = DayLogActivities(db_pool=clean_db, settings=vault["settings"])
    got = await ActivityEnvironment().run(acts.gather_daylogs, "2019-03-11", "2019-03-13")
    assert [e["date"] for e in got] == ["2019-03-11", "2019-03-12"]
    assert "Knowledge row for the 11th." in got[0]["text"]
    assert "From the vault journal." in got[1]["text"]
    assert "Old knowledge row" not in got[1]["text"]
    assert "c2VjcmV0" not in got[1]["text"], "ciphertext would reach the rollup's model"
