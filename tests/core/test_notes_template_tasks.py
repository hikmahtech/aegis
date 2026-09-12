"""A note Raphael creates from the vault's template carries no open tasks.

The 2026-09-12 journal backfill rendered the user's daily/weekly/monthly
templates verbatim, so 49 notes arrived with 125 unticked prompts ("- [ ] #admin
Plan the day") for days that had already passed — and obsidian-checklist-plugin
lists every open box in the vault. A new note now keeps the template's layout
and drops its open tasks; a note that already exists is never touched.
"""

from __future__ import annotations

from datetime import date, datetime

from aegis.services import notes

from tests.notes_vault import DAILY_TEMPLATE, make_vault, needs_git, remote_file

TASKY_TEMPLATE = (
    DAILY_TEMPLATE
    + "### Todo\n"
    + "#todo \n"
    + "- [ ] #admin Plan the day\n"
    + "  - [ ] an indented prompt\n"
    + "* [ ] a star bullet\n"
    + "- [x] a box the template ships ticked\n"
    + "- [ ] \n"
)


def test_drop_open_tasks_keeps_everything_but_unticked_boxes():
    out = notes.drop_open_tasks(TASKY_TEMPLATE)
    assert "[ ]" not in out
    assert "- [x] a box the template ships ticked" in out
    assert "### Todo" in out
    assert "## Journal" in out


def test_drop_open_tasks_leaves_task_like_prose_alone():
    text = "Checked the [ ] notation in the docs.\n- a plain bullet\n"
    assert notes.drop_open_tasks(text) == text


@needs_git
def test_a_new_journal_note_carries_no_open_tasks(tmp_path):
    files = {
        "_templates/{{tp_title_today}}.md": TASKY_TEMPLATE,
        ".obsidian/app.json": "{}\n",
    }
    v = make_vault(tmp_path, files)
    day = date(2026, 9, 11)
    ap = notes.journal_append("daily", day, day.isoformat(), "Raphael's day.", datetime(2026, 9, 12, 13, 10))
    notes.write_sync(v["cfg"], [ap], "journal")

    text = remote_file(v, notes.daily_note_path(day))
    assert text, "the note was not pushed"
    assert "[ ]" not in text
    assert "### Todo" in text
    assert "- [x] a box the template ships ticked" in text
    assert "## Raphael" in text and "Raphael's day." in text


@needs_git
def test_the_users_own_open_tasks_survive_an_append(tmp_path):
    user_note = "# Sep 11, 2026\n### Todo\n- [ ] call the plumber\n"
    files = {
        "_templates/{{tp_title_today}}.md": TASKY_TEMPLATE,
        "journal/11 Sep 26.md": user_note,
        ".obsidian/app.json": "{}\n",
    }
    v = make_vault(tmp_path, files)
    day = date(2026, 9, 11)
    ap = notes.journal_append("daily", day, day.isoformat(), "Raphael's day.", datetime(2026, 9, 12, 13, 10))
    notes.write_sync(v["cfg"], [ap], "journal")

    text = remote_file(v, notes.daily_note_path(day))
    assert text.startswith(user_note), "append-only: the user's text must come first, unchanged"
    assert "- [ ] call the plumber" in text
    assert "Raphael's day." in text
