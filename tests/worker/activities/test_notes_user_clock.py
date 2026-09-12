"""The worker's vault writes date on the user's clock (#514, from the audit):
the journal entry's time and the research answer's dated section follow
`user_timezone`, not the container's UTC."""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from aegis.services import notes
from aegis_worker.activities.notes import NotesActivities
from aegis_worker.activities.research import ResearchActivities
from temporalio.testing import ActivityEnvironment

from tests.notes_vault import make_vault, needs_git

ZONE = "Pacific/Kiritimati"


@pytest_asyncio.fixture(loop_scope="function")
async def user_zone(db_pool):
    before = await db_pool.fetchval("SELECT value FROM settings WHERE key = 'user_timezone'")
    await db_pool.execute(
        "INSERT INTO settings (key, value) VALUES ('user_timezone', $1) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        ZONE,
    )
    yield db_pool
    if before is None:
        await db_pool.execute("DELETE FROM settings WHERE key = 'user_timezone'")
    else:
        await db_pool.execute("UPDATE settings SET value = $1 WHERE key = 'user_timezone'", before)


@needs_git
@pytest.mark.asyncio
async def test_the_journal_write_uses_the_users_clock(user_zone, tmp_path, monkeypatch):
    vault = make_vault(tmp_path)
    seen: dict = {}
    real = notes.journal_append

    def spy(kind, day, label, body, now):
        seen["now"] = now
        return real(kind, day, label, body, now)

    monkeypatch.setattr(notes, "journal_append", spy)
    acts = NotesActivities(settings=vault["settings"], db_pool=user_zone)
    out = await ActivityEnvironment().run(
        acts.notes_journal_write,
        {"kind": "daily", "day": "2026-09-14", "label": "2026-09-14", "text": "A day."},
    )
    assert out["status"] == "written"
    assert seen["now"].tzinfo == ZoneInfo(ZONE)


@needs_git
@pytest.mark.asyncio
async def test_a_research_answer_is_dated_on_the_users_clock(user_zone, tmp_path, monkeypatch):
    vault = make_vault(tmp_path)
    seen: dict = {}
    real = notes.question_append

    def spy(question, report, asked):
        seen["asked"] = asked
        return real(question, report, asked)

    monkeypatch.setattr(notes, "question_append", spy)
    acts = ResearchActivities(settings=vault["settings"], db_pool=user_zone)
    out = await acts._save_to_vault("Why is the sky blue?", "Rayleigh scattering [1].", [])
    assert out is not None
    assert seen["asked"].tzinfo == ZoneInfo(ZONE)
