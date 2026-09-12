"""The weekly journal backfill looks only at recent rows (#514, from the audit).

The daylog's pre-vault rows are still in the knowledge store. If the weekly
run reread all of them, a block the user deleted from an old journal note on
the phone would be back the next Sunday — against the rule that the user's
edits to the vault stand. The schedule passes `since_days`; a run started by
hand passes 0 and takes every row."""

from __future__ import annotations

from uuid import uuid4

import pytest_asyncio
from aegis.services import notes
from aegis_worker.activities.notes import NotesActivities
from temporalio.testing import ActivityEnvironment

from tests.notes_vault import make_vault, needs_git, remote_file

OLD_DAY = "2019-03-11"
RECENT_DAY = "2019-03-12"


@pytest_asyncio.fixture(loop_scope="function")
async def rows(db_pool):
    async def wipe():
        await db_pool.execute(
            "DELETE FROM knowledge_content WHERE source_type IN ('daylog', 'daylog_rollup')"
        )

    await wipe()
    # One row filed 30 days ago (a pre-vault day), one filed now (a fallback day).
    for day, age_days in ((OLD_DAY, 30), (RECENT_DAY, 0)):
        cid = f"bw-{uuid4().hex[:8]}"
        await db_pool.execute(
            "INSERT INTO knowledge_content (content_id, url, title, source_type, metadata, ingested_at) "
            "VALUES ($1, $2, 'Day Log', 'daylog', $3, now() - make_interval(days => $4))",
            cid,
            f"aegis://daylog/{cid}",
            {"date": day},
            age_days,
        )
        await db_pool.execute(
            "INSERT INTO knowledge_chunks (content_id, chunk_index, chunk_text) VALUES ($1, 0, $2)",
            cid,
            f"The day {day}.",
        )
    yield db_pool
    await wipe()


@needs_git
async def test_the_scheduled_run_skips_old_rows_and_a_hand_run_takes_them(tmp_path, rows):
    vault = make_vault(tmp_path)
    acts = NotesActivities(settings=vault["settings"], db_pool=rows)

    weekly = await ActivityEnvironment().run(acts.notes_backfill_journal, 1000, 14)
    assert (weekly["entries"], weekly["written"]) == (1, 1)
    assert "The day 2019-03-12." in remote_file(vault, notes.daily_note_path(notes.date(2019, 3, 12)))
    assert remote_file(vault, notes.daily_note_path(notes.date(2019, 3, 11))) == "", (
        "an old row must not come back on a scheduled run"
    )

    by_hand = await ActivityEnvironment().run(acts.notes_backfill_journal, 1000, 0)
    assert (by_hand["entries"], by_hand["written"], by_hand["already_there"]) == (2, 1, 1)
    assert "The day 2019-03-11." in remote_file(vault, notes.daily_note_path(notes.date(2019, 3, 11)))


@needs_git
async def test_a_call_with_only_a_limit_still_takes_every_row(tmp_path, rows):
    """A call queued before `since_days` existed passes the limit alone."""
    vault = make_vault(tmp_path)
    acts = NotesActivities(settings=vault["settings"], db_pool=rows)
    out = await ActivityEnvironment().run(acts.notes_backfill_journal, 1000)
    assert out["entries"] == 2
