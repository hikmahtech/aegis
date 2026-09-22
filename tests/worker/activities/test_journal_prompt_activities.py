"""The journal gap prompt's activities (vault record spec §3), against a
throwaway vault (local bare git repo) and the real test database. Every note
is made up here."""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aegis.services import notes
from aegis.services import vault_layout as vl
from aegis.services.settings_store import put_setting
from aegis_worker.activities.notes import NotesActivities
from temporalio.testing import ActivityEnvironment

from tests.notes_vault import CIPHER, DAILY_TEMPLATE, SEED_FILES, make_vault, needs_git

TEMPLATE = DAILY_TEMPLATE + "- [ ] #admin Plan the day\nGrateful for: <% tp.file.cursor() %>\n"
FILES = {
    **SEED_FILES,
    "_templates/{{tp_title_today}}.md": TEMPLATE,
    # 14th: periodic-notes made the day's note on the device; nobody wrote in it.
    "journal/14 Sep 26.md": (
        '---\naliases: ["14-09-2026", "Sep 14, 2026"]\ndate: "2026-09-14"\n'
        'time: "07:02"\ntags: ["journal", "daily"]\n---\n'
        "# Sep 14, 2026\n## Journal\n- \n- [ ] #admin Plan the day\nGrateful for: \n"
    ),
    "journal/2026/09. Sep/16 Sep 26.md": (
        "# Sep 16, 2026\n## Journal\n- [x] #admin Plan the day\n- [ ] call the bank\n"
    ),
    "journal/17 Sep 26.md": "# Sep 17, 2026\n## Journal\n- walked to the lake with the kids\n",
    "journal/2026/09. Sep/18 Sep 26.md": (
        "# Sep 18, 2026\n## Journal\n"
        "- #aegis/sebas in my words %% aegis:selfreport:2026-09-18 %%\n\t- A long day.\n"
    ),
    "journal/2026/09. Sep/20 Sep 26.md": f"# Sep 20, 2026\n## Journal\n- {CIPHER}\n",
}


@pytest_asyncio.fixture(loop_scope="function")
async def rows(db_pool):
    """This file's own interactions and no `vault_layout` row (files in this
    package share one database under `--dist loadfile`)."""

    async def wipe():
        await db_pool.execute("DELETE FROM interactions WHERE flow_run_id LIKE 'jp-test-%'")
        await db_pool.execute("DELETE FROM settings WHERE key = 'vault_layout'")
        vl.invalidate_cache()

    await db_pool.execute(
        "INSERT INTO agents (id, name, role, system_prompt_path, active) "
        "VALUES ('sebas', 'Sebas', 'assistant', 'personalities/sebas', TRUE) "
        "ON CONFLICT (id) DO NOTHING"
    )
    await wipe()
    yield db_pool
    await wipe()


# ---------------------------------------------------------------- the gap check


@needs_git
@pytest.mark.parametrize(
    ("day", "status", "words"),
    [
        ("2026-09-14", "gap", 0),  # the template, rendered on the device, alone (live note)
        ("2026-09-15", "gap", 0),  # the day log's block in a note AEGIS made from the template
        ("2026-09-16", "gap", 0),  # checkboxes only
        ("2026-09-17", "wrote", 7),  # the live root note, with the user's words
        ("2026-09-18", "answered", 0),  # the self-report is already there
        ("2026-09-19", "gap", 0),  # no note at all
        ("2026-09-20", "encrypted", 2),  # the user wrote and encrypted it
    ],
)
async def test_the_gap_check_reads_the_days_notes(tmp_path, day, status, words):
    vault = make_vault(tmp_path, FILES)
    notes.write_sync(
        vault["cfg"],
        [
            notes.journal_append(
                "daily",
                date(2026, 9, 15),
                "2026-09-15",
                "Shipped the release.\n\nMet the team.",
                datetime(2026, 9, 16, 0, 30),
            )
        ],
        "journal",
    )
    acts = NotesActivities(settings=vault["settings"])
    got = await ActivityEnvironment().run(acts.journal_gap_check, day, 5)
    assert (got["status"], got["words"], got["day"]) == (status, words, day)


@needs_git
async def test_a_failed_pull_sends_no_card(tmp_path):
    vault = make_vault(tmp_path, FILES)
    acts = NotesActivities(settings=vault["settings"])
    first = await ActivityEnvironment().run(acts.journal_gap_check, "2026-09-19", 5)
    assert first["status"] == "gap" and first["day_name"] == "Saturday 19 September"
    vault["remote"].rename(tmp_path / "remote.moved")
    again = await ActivityEnvironment().run(acts.journal_gap_check, "2026-09-19", 5)
    assert again == {"status": "unreadable", "day": "2026-09-19"}, (
        "a stale copy cannot say the user wrote nothing"
    )


async def test_no_vault_no_card():
    got = await ActivityEnvironment().run(
        NotesActivities(settings=SimpleNamespace()).journal_gap_check, "2026-09-19", 5
    )
    assert got == {"status": "not_configured", "day": "2026-09-19"}


@needs_git
async def test_daily_notes_switched_off_send_no_card(tmp_path, rows):
    await put_setting(rows, "vault_layout", {"daily": {"enabled": False}})
    vl.invalidate_cache()
    vault = make_vault(tmp_path, FILES)
    acts = NotesActivities(settings=vault["settings"], db_pool=rows)
    got = await ActivityEnvironment().run(acts.journal_gap_check, "2026-09-19", 5)
    assert got == {"status": "disabled", "day": "2026-09-19"}
