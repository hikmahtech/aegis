"""The journal gap prompt's activities (vault record spec §3), against a
throwaway vault (local bare git repo) and the real test database. Every note
is made up here."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from aegis.services import journal_prompt as jp
from aegis.services import notes
from aegis.services import vault_layout as vl
from aegis.services.settings_store import put_setting
from aegis_worker.activities.daylog import DayLogActivities
from aegis_worker.activities.notes import NotesActivities
from temporalio.testing import ActivityEnvironment

from tests.notes_vault import (
    CIPHER,
    DAILY_TEMPLATE,
    SEED_FILES,
    make_vault,
    needs_git,
    remote_file,
    remote_head,
)

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


# ---------------------------------------------------------- filing the answer

DAY = "2026-09-21"
PATH = "journal/2026/09. Sep/21 Sep 26.md"
META = {"day": DAY, "agent_id": "sebas"}


async def _answered_card(pool, day: str, text: str, origin: str = jp.ORIGIN) -> str:
    return str(
        await pool.fetchval(
            "INSERT INTO interactions (flow_run_id, agent_id, kind, origin, prompt, status, "
            " response, metadata, resolved_at) "
            "VALUES ($1, 'sebas', 'input', $2, 'Your day?', 'resolved', $3, $4, now()) "
            "RETURNING id",
            f"jp-test-{uuid4()}",
            origin,
            {"value": text},
            {"day": day, "agent_id": "sebas"},
        )
    )


async def _response(pool, iid):
    return await pool.fetchval("SELECT response FROM interactions WHERE id = $1::uuid", iid)


@needs_git
async def test_an_answer_is_filed_as_written_and_then_blanked(tmp_path, rows):
    vault = make_vault(tmp_path, FILES)
    acts = NotesActivities(settings=vault["settings"], db_pool=rows)
    answer = "Walked to the lake.\nCalled my sister."
    iid = await _answered_card(rows, DAY, answer)

    res = await ActivityEnvironment().run(acts.file_journal_answer, iid, {"value": answer}, META)

    assert res == {"status": "written", "path": PATH}
    text = remote_file(vault, PATH)
    assert "- #aegis/sebas in my words %% aegis:selfreport:2026-09-21 %%\n" in text
    assert "\t- Walked to the lake.\n\t- Called my sister.\n" in text, "one bullet per line"
    assert await _response(rows, iid) == {"value": "", "filed": PATH}
    # The check now says the day is answered, and a second filing writes nothing.
    check = await ActivityEnvironment().run(acts.journal_gap_check, DAY, 5)
    assert check["status"] == "answered"
    head = remote_head(vault)
    again = await ActivityEnvironment().run(acts.file_journal_answer, iid, {"value": answer}, META)
    assert again == {"status": "exists", "path": PATH} and remote_head(vault) == head


@needs_git
async def test_an_empty_answer_files_nothing(tmp_path, rows):
    vault = make_vault(tmp_path, FILES)
    acts = NotesActivities(settings=vault["settings"], db_pool=rows)
    head = remote_head(vault)
    for response in ({"value": ""}, {"value": "  \n "}, {}):
        res = await ActivityEnvironment().run(acts.file_journal_answer, "x", response, META)
        assert res == {"status": "empty"}
    assert remote_head(vault) == head


@needs_git
async def test_a_failed_filing_is_filed_and_blanked_by_the_weekly_sweep(tmp_path, rows):
    vault = make_vault(tmp_path, FILES)
    acts = NotesActivities(settings=vault["settings"], db_pool=rows)
    iid = await _answered_card(rows, DAY, "A day at home.")
    moved = tmp_path / "remote.moved"
    vault["remote"].rename(moved)

    first = await ActivityEnvironment().run(
        acts.file_journal_answer, iid, {"value": "A day at home."}, META
    )
    assert first["status"] == "error"
    assert await _response(rows, iid) == {"value": "A day at home."}, (
        "the words stay until the vault holds them"
    )

    moved.rename(vault["remote"])
    swept = await ActivityEnvironment().run(acts.notes_file_answers, 14)
    assert swept == {"status": "ok", "answers": 1, "filed": 1, "failed": 0}
    assert await _response(rows, iid) == {"value": "", "filed": PATH}
    assert "A day at home." in remote_file(vault, PATH)
    again = await ActivityEnvironment().run(acts.notes_file_answers, 14)
    assert again == {"status": "ok", "answers": 0, "filed": 0, "failed": 0}


@needs_git
async def test_no_log_line_carries_the_answer(tmp_path, rows, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    secret = "zebra walnut origami"
    vault = make_vault(tmp_path, FILES)
    acts = NotesActivities(settings=vault["settings"], db_pool=rows)
    iid = await _answered_card(rows, DAY, secret)
    moved = tmp_path / "remote.moved"
    vault["remote"].rename(moved)
    await ActivityEnvironment().run(acts.file_journal_answer, iid, {"value": secret}, META)
    moved.rename(vault["remote"])
    await ActivityEnvironment().run(acts.notes_file_answers, 14)
    await ActivityEnvironment().run(acts.journal_gap_check, DAY, 5)
    out = capsys.readouterr()
    assert secret in remote_file(vault, PATH), "the premise: the words did go somewhere"
    assert secret not in caplog.text + out.out + out.err


async def test_the_day_log_leaves_the_journal_card_out_of_its_decisions(rows):
    await _answered_card(rows, DAY, "a private day")
    await _answered_card(rows, DAY, "Acme is my gym", origin="curiosity")
    now = datetime.now(UTC)
    got = await DayLogActivities(db_pool=rows)._source_decisions(
        now - timedelta(minutes=5), now + timedelta(minutes=5)
    )
    origins = [d["origin"] for d in got]
    assert "curiosity" in origins, "the premise: the window holds this file's cards"
    assert jp.ORIGIN not in origins
