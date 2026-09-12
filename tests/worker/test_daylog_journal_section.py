"""A weekly or monthly rollup reads Raphael's own section of each journal note
first (#514 validation).

The section is appended at the END of the note. Clipping the whole note from
the top dropped Raphael's narrative whenever the template plus the user's own
writing ran past the clip."""

from __future__ import annotations

from datetime import date, datetime

import pytest_asyncio
from aegis.services import notes
from aegis_worker.activities.daylog import _ROLLUP_ENTRY_CLIP, DayLogActivities
from temporalio.testing import ActivityEnvironment

from tests.notes_vault import device_commit, make_vault, needs_git
from tests.worker.test_daylog import _wipe


@pytest_asyncio.fixture(loop_scope="function")
async def clean_db(db_pool):
    async with db_pool.acquire() as conn:
        await _wipe(conn)
    yield db_pool
    async with db_pool.acquire() as conn:
        await _wipe(conn)


@needs_git
async def test_a_long_note_still_gives_the_rollup_raphaels_narrative(clean_db, tmp_path):
    vault = make_vault(tmp_path)
    mine = "My own long notes about the day. " * 120  # ~4,000 chars, past the clip
    device_commit(vault, {"journal/12 Mar 19.md": f"# 12 Mar 19\n{mine}\n"})
    notes.write_sync(
        vault["cfg"],
        [
            notes.journal_append(
                "daily", date(2019, 3, 12), "2019-03-12", "Raphael's narrative of the day.",
                datetime(2019, 3, 12, 21, 0),
            )
        ],
        "journal",
    )

    acts = DayLogActivities(db_pool=clean_db, settings=vault["settings"])
    got = await ActivityEnvironment().run(acts.gather_daylogs, "2019-03-12", "2019-03-12")
    assert [e["date"] for e in got] == ["2019-03-12"]
    text = got[0]["text"]
    assert text.startswith("Raphael's narrative of the day."), "Raphael's section was clipped away"
    assert "My own long notes" in text, "the user's writing should follow, as room allows"
    assert len(text) <= _ROLLUP_ENTRY_CLIP
