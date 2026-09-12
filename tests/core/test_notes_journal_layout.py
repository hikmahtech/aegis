"""The journal follows the vault's own layout (the user's request, 2026-09-12).

The first version put every note at the `journal/` root, dated weeks from their
Sunday, put the month note loose in the year folder and appended a trailing
`## Raphael` section. The vault files notes in `journal/<YYYY>/<NN. Mon>/`,
dates weeks from their Monday, keeps the month as that folder's folder note,
and writes entries as `- ` bullets under the note's own `## Journal`. These
tests pin the vault's convention and the insert-only rule that goes with it.
No personal note content: every fixture is made up here.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from aegis.services import notes

from tests.notes_vault import device_commit, git, make_vault, needs_git, remote_file

DAILY_WITH_SECTIONS = (
    "---\n"
    'date: "{{date}}"\n'
    "---\n"
    "# {{date:MMM DD, YYYY}}\n"
    "---\n"
    "## Journal\n"
    "#journal\n"
    "- \n"
    "---\n"
    "### Review \n"
    "#review\n"
    "- \n"
    "---\n"
    "### Todo\n"
    "- [ ] #admin Plan the day\n"
)


def _daily(day: date, body: str = "The day, as Raphael saw it.") -> notes.Append:
    return notes.journal_append("daily", day, day.isoformat(), body, datetime(2026, 9, 12, 19, 5))


# ---------------------------------------------------------------- the block


def test_the_block_is_a_tagged_bullet_with_one_child_per_paragraph():
    block = notes.journal_block(
        "daylog:2026-09-11",
        "day log",
        "First line\nstill first.\n\nSecond.\n\n### Highlights\n- a\n- b",
    )
    assert block == (
        "- #raphael day log %% aegis:daylog:2026-09-11 %%\n"
        "\t- First line still first.\n"
        "\t- Second.\n"
        "\t- **Highlights**\n"
        "\t- a\n"
        "\t- b\n"
    )


# The daylog's deterministic fallback (`_format_daylog_fallback`): labels with
# two-space items, and a decision whose prompt is a whole review card, so blank
# lines and `•` items sit inside it. The shape is the real one; the item text
# is made up.
FALLBACK_BODY = (
    "Day log for 2026-08-01.\n"
    "Met / attended:\n"
    "  - Team lunch\n"
    "  - Standup: morning\n"
    "Completed:\n"
    "  - Invoices/Receipts filed\n"
    "Decided:\n"
    "  - ☀ <b>Daily review</b> — Sat 01 Aug\n"
    "\n"
    "📥 <b>Inbox</b>: 41 open\n"
    "  • Renew the domain before it lapses\n"
    "  • Backup job failed\n"
    "\n"
    "📅 <b>Today / overdue</b>: 9\n"
    "  • Ch -> reviewed\n"
    "Captured / clarified:\n"
    "  - Invoices/Receipts filed [reference]\n"
)
FALLBACK_BLOCK = (
    "- #raphael day log %% aegis:daylog:2026-08-01 %%\n"
    "\t- Day log for 2026-08-01.\n"
    "\t- Met / attended:\n"
    "\t\t- Team lunch\n"
    "\t\t- Standup: morning\n"
    "\t- Completed:\n"
    "\t\t- Invoices/Receipts filed\n"
    "\t- Decided:\n"
    "\t\t- ☀ <b>Daily review</b> — Sat 01 Aug\n"
    "\t- 📥 <b>Inbox</b>: 41 open\n"
    "\t\t- Renew the domain before it lapses\n"
    "\t\t- Backup job failed\n"
    "\t- 📅 <b>Today / overdue</b>: 9\n"
    "\t\t- Ch -> reviewed\n"
    "\t- Captured / clarified:\n"
    "\t\t- Invoices/Receipts filed [reference]\n"
)


def test_the_fallback_day_log_keeps_its_outline():
    """Labels at depth 1 with their items under them, the card's lines at depth
    1 with their `•` items under them, and nothing glued together."""
    assert notes.journal_block("daylog:2026-08-01", "day log", FALLBACK_BODY) == FALLBACK_BLOCK


def test_the_fallback_reads_back_with_its_outline_and_round_trips():
    key = "daylog:2026-08-01"
    mine, _ = notes.split_section(f"## Journal\n#journal\n{FALLBACK_BLOCK}\n## Notes\n", key)
    assert mine == (
        "Day log for 2026-08-01.\n\n"
        "Met / attended:\n  - Team lunch\n  - Standup: morning\n\n"
        "Completed:\n  - Invoices/Receipts filed\n\n"
        "Decided:\n  - ☀ <b>Daily review</b> — Sat 01 Aug\n\n"
        "📥 <b>Inbox</b>: 41 open\n  - Renew the domain before it lapses\n  - Backup job failed\n\n"
        "📅 <b>Today / overdue</b>: 9\n  - Ch -> reviewed\n\n"
        "Captured / clarified:\n  - Invoices/Receipts filed [reference]"
    )
    # What the rollups read back lays out the same way again.
    assert notes.journal_block(key, "day log", mine) == FALLBACK_BLOCK


def test_a_prose_narrative_is_one_child_per_paragraph():
    body = (
        "Over the week the owner shipped the migration.\n\n"
        "Most of Tuesday went on the importer, which\nran out of memory twice.\n\n"
        "By Friday the open threads were the invoice and the backup."
    )
    assert notes.journal_block("daylog:weekly:2026-W37", "week in review", body) == (
        "- #raphael week in review %% aegis:daylog:weekly:2026-W37 %%\n"
        "\t- Over the week the owner shipped the migration.\n"
        "\t- Most of Tuesday went on the importer, which ran out of memory twice.\n"
        "\t- By Friday the open threads were the invoice and the backup.\n"
    )


def test_a_wrapped_paragraph_joins_and_stops_at_an_item_or_a_label():
    body = "A wrapped\nparagraph here.\n  - an indented item\nNext:\n  • one\nlast line\ncontinues"
    assert notes.body_outline(body) == [
        (1, "A wrapped paragraph here."),
        (2, "an indented item"),
        (1, "Next:"),
        (2, "one"),
        (1, "last line continues"),
    ]


def test_depth_follows_indentation_and_stops_at_four():
    body = "Top:\n  - a\n    - b\n\t\t\t- c\n        - d\n          - e\n      - f\n\t- g"
    assert notes.body_outline(body) == [
        (1, "Top:"),
        (2, "a"),
        (3, "b"),
        (4, "c"),
        (4, "d"),
        (4, "e"),
        (4, "f"),
        (2, "g"),
    ]


def test_the_block_cannot_forge_another_marker():
    block = notes.journal_block("daylog:2026-09-11", "day log", "see %% aegis:daylog:x %%")
    assert "%% aegis:daylog:x %%" not in block


def test_split_section_reads_the_block_back_and_leaves_the_users_lines():
    key = notes.journal_key("daily", "2026-09-11")
    note = (
        "## Journal\n#journal\n- mine\n"
        f"- #raphael day log {notes.marker(key)}\n\t- Para one.\n\t- Para two.\n"
        "---\n### Review \n- r\n"
    )
    mine, rest = notes.split_section(note, key)
    assert mine == "Para one.\n\nPara two."
    assert "- mine" in rest and "### Review" in rest
    assert "#raphael" not in rest and "Para one" not in rest


# ------------------------------------------------------------- insertion


def test_a_daily_entry_goes_at_the_end_of_the_users_journal_section():
    existing = (
        "# Sep 11, 2026\n---\n## Journal\n#journal\n- mine one\n- mine two\n\n---\n"
        "### Review \n#review\n- reviewed\n"
    )
    ap = _daily(date(2026, 9, 11))
    out = notes.append_text(existing, ap)
    assert notes.is_one_insertion(existing, out)
    assert out.index("- mine two") < out.index("- #raphael day log") < out.index("---\n### Review")
    assert out.count("- reviewed") == 1


@pytest.mark.parametrize(
    ("kind", "day", "label", "section"),
    [
        ("weekly", date(2026, 9, 7), "2026-W37", "### Review"),
        ("monthly", date(2026, 8, 1), "2026-08", "### Month Review"),
    ],
)
def test_weekly_and_monthly_entries_go_into_their_review_sections(kind, day, label, section):
    existing = f"# note\n### Top 3\n- x\n{section}\n- mine\n---\n```ccard\ntype: x\n```\n"
    ap = notes.journal_append(kind, day, label, "Review text.", datetime(2026, 9, 12, 19, 5))
    out = notes.append_text(existing, ap)
    assert notes.is_one_insertion(existing, out)
    assert out.index(section) < out.index("- mine") < out.index("- #raphael") < out.index("---")


def test_a_note_without_the_section_gets_it_at_the_end():
    existing = "# Sep 11, 2026\nno sections here"
    out = notes.append_text(existing, _daily(date(2026, 9, 11)))
    assert notes.is_one_insertion(existing, out)
    assert out.endswith(
        "\n## Journal\n- #raphael day log %% aegis:daylog:2026-09-11 %%\n\t- The day, as Raphael saw it.\n"
    )


@pytest.mark.parametrize(
    ("kind", "day", "label", "heading"),
    [
        ("daily", date(2026, 9, 11), "2026-09-11", "#### Journal "),
        ("weekly", date(2026, 9, 7), "2026-W37", "## Review"),
        ("monthly", date(2026, 8, 1), "2026-08", "## Review"),
        ("monthly", date(2026, 8, 1), "2026-08", "### Month Review"),
    ],
)
def test_the_section_is_found_by_its_text_at_any_heading_level(kind, day, label, heading):
    existing = f"# note\n## Highlights\n- h\n{heading}\n#review\n- mine\n\n## Notes\n- n\n"
    ap = notes.journal_append(kind, day, label, "Text.", datetime(2026, 9, 12, 19, 5))
    out = notes.append_text(existing, ap)
    assert notes.is_one_insertion(existing, out)
    assert (
        out.index(heading) < out.index("- mine") < out.index("- #raphael") < out.index("## Notes")
    )


def test_a_month_note_prefers_review_to_month_review():
    existing = "# 08. Aug Monthly\n### Month Review\n- old\n## Review\n- new\n"
    ap = notes.journal_append(
        "monthly", date(2026, 8, 1), "2026-08", "Text.", datetime(2026, 9, 12, 19, 5)
    )
    out = notes.append_text(existing, ap)
    assert out.index("- new") < out.index("- #raphael month in review")


def test_a_missing_review_section_is_added_as_a_level_two_heading():
    existing = "# Week of Sep 07, 2026\n## Highlights\n- h\n"
    ap = notes.journal_append(
        "weekly", date(2026, 9, 7), "2026-W37", "Text.", datetime(2026, 9, 12, 19, 5)
    )
    out = notes.append_text(existing, ap)
    assert notes.is_one_insertion(existing, out)
    assert out.endswith(
        "\n## Review\n- #raphael week in review %% aegis:daylog:weekly:2026-W37 %%\n\t- Text.\n"
    )


def test_the_month_review_ends_at_the_folder_card_fence():
    """The monthly template closes on a `ccard` code fence: the block lands in
    the Review section, never after (or inside) the card."""
    for card in (
        "## In this folder\n```ccard\ntype: folder_brief_live\n```\n",
        "```ccard\ntype: folder_brief_live\n```\n",
    ):
        existing = f"# 08. Aug Monthly\n## Review\n#review\n- mine\n\n{card}"
        ap = notes.journal_append(
            "monthly", date(2026, 8, 1), "2026-08", "Text.", datetime(2026, 9, 12, 19, 5)
        )
        out = notes.append_text(existing, ap)
        assert notes.is_one_insertion(existing, out)
        assert out.index("- mine") < out.index("- #raphael") < out.index("```ccard")
        assert out.endswith(card)


# The vault's templates as redesigned on 2026-09-12: every section `##`, no
# `---` rules, no checkboxes, and the month's folder card at the end.
NEW_TEMPLATES = {
    "daily": (
        "---\n"
        'aliases: ["{{date:DD-MM-YYYY}}", "{{date:MMM DD, YYYY}}", "{{date:MMM DD YY}}"]\n'
        'date: "{{date}}"\n'
        'tags: ["journal", "daily"]\n'
        "---\n"
        "# {{date:dddd, MMM DD, YYYY}}\n\n"
        "## Journal\n#journal\n- \n\n"
        "## Notes\n- \n\n"
        "## Review\n#review\n- \n"
    ),
    "weekly": (
        "---\n"
        'aliases: ["weekly-{{date:DD-MM-YYYY}}", "weekly-{{date:MMM DD, YYYY}}", "weekly-{{date:MMM DD YY}}"]\n'
        'date: "{{date}}"\n'
        'tags: ["journal", "weekly"]\n'
        "---\n"
        "# Week of {{date:MMM DD, YYYY}}\n\n"
        "## Highlights\n- \n\n"
        "## Review\n#review\n- \n"
    ),
    "monthly": (
        "# {{title}} Monthly\n\n"
        "## Highlights\n- \n\n"
        "## Review\n#review\n- \n\n"
        "## In this folder\n```ccard\ntype: folder_brief_live\n```\n"
    ),
}


@pytest.mark.parametrize(
    ("kind", "day", "label", "expected"),
    [
        (
            "daily",
            date(2026, 9, 11),
            "2026-09-11",
            "# Friday, Sep 11, 2026\n\n## Journal\n#journal\n"
            "- #raphael day log %% aegis:daylog:2026-09-11 %%\n\t- Text.\n\n"
            "## Notes\n- \n\n## Review\n#review\n- \n",
        ),
        (
            "weekly",
            date(2026, 9, 9),
            "2026-W37",
            "# Week of Sep 07, 2026\n\n## Highlights\n- \n\n## Review\n#review\n"
            "- #raphael week in review %% aegis:daylog:weekly:2026-W37 %%\n\t- Text.\n",
        ),
        (
            "monthly",
            date(2026, 8, 1),
            "2026-08",
            "# 08. Aug Monthly\n\n## Highlights\n- \n\n## Review\n#review\n"
            "- #raphael month in review %% aegis:daylog:monthly:2026-08 %%\n\t- Text.\n\n"
            "## In this folder\n```ccard\ntype: folder_brief_live\n```\n",
        ),
    ],
)
def test_a_new_note_from_the_current_templates(tmp_path, kind, day, label, expected):
    (tmp_path / "_templates").mkdir()
    (tmp_path / notes.TEMPLATES[kind]).write_text(NEW_TEMPLATES[kind], "utf-8")
    cfg = notes.NotesConfig(path=tmp_path)
    ap = notes.journal_append(kind, day, label, "Text.", datetime(2026, 9, 12, 19, 5))
    text = notes.append_text(None, ap, notes._new_note_text(cfg, ap))
    body = text.split("---\n", 2)[2] if text.startswith("---\n") else text
    assert body == expected


def test_is_one_insertion():
    assert notes.is_one_insertion("abc", "abXYc")
    assert notes.is_one_insertion("abc", "abc tail")
    assert notes.is_one_insertion("", "anything")
    assert not notes.is_one_insertion("abc", "aXc"), "a changed character"
    assert not notes.is_one_insertion("abc", "ac"), "a deleted character"
    assert not notes.is_one_insertion("a\nb\n", "b\na\nX\n"), "reordered lines"
    assert not notes.is_one_insertion("abcd", "aXbcYd"), "two insertions"


def test_a_new_note_loses_only_the_target_sections_placeholder(tmp_path):
    (tmp_path / "_templates").mkdir()
    (tmp_path / notes.TEMPLATES["daily"]).write_text(DAILY_WITH_SECTIONS, "utf-8")
    cfg = notes.NotesConfig(path=tmp_path)
    ap = _daily(date(2026, 9, 11))
    text = notes.append_text(None, ap, notes._new_note_text(cfg, ap))
    journal = text[text.index("## Journal") : text.index("### Review")]
    assert "\n- \n" not in journal, "the empty placeholder above the entry is gone"
    review = text[text.index("### Review") : text.index("### Todo")]
    assert "\n- \n" in review, "other sections keep their template lines"
    assert "[ ]" not in text


# --------------------------------------------------------------- git trips


@needs_git
def test_a_day_without_a_live_note_is_filed_in_its_month_folder(tmp_path):
    v = make_vault(tmp_path)
    res = notes.write_sync(v["cfg"], [_daily(date(2026, 9, 13))], "journal")
    assert res["outcomes"] == [{"path": "journal/2026/09. Sep/13 Sep 26.md", "changed": True}]
    assert "- #raphael day log" in remote_file(v, "journal/2026/09. Sep/13 Sep 26.md")
    assert remote_file(v, "journal/13 Sep 26.md") == ""


@needs_git
def test_a_live_root_note_wins_and_a_filed_one_is_not_created(tmp_path):
    v = make_vault(tmp_path)  # seeds journal/12 Sep 26.md at the root
    res = notes.write_sync(v["cfg"], [_daily(date(2026, 9, 12))], "journal")
    assert res["outcomes"] == [{"path": "journal/12 Sep 26.md", "changed": True}]
    assert remote_file(v, "journal/2026/09. Sep/12 Sep 26.md") == ""


@needs_git
def test_a_marker_in_the_filed_note_counts_once_the_user_files_the_root_note(tmp_path):
    v = make_vault(tmp_path)
    notes.write_sync(v["cfg"], [_daily(date(2026, 9, 12))], "journal")
    # The user files the day's note into its month folder from the phone.
    git("pull", "-q", cwd=v["device"])
    (v["device"] / "journal/2026/09. Sep").mkdir(parents=True)
    git("mv", "journal/12 Sep 26.md", "journal/2026/09. Sep/12 Sep 26.md", cwd=v["device"])
    device_commit(v, {}, "file the note")
    again = notes.write_sync(v["cfg"], [_daily(date(2026, 9, 12), "a retried body")], "journal")
    assert again["status"] == "exists"
    assert again["outcomes"] == [{"path": "journal/2026/09. Sep/12 Sep 26.md", "changed": False}]
    assert remote_file(v, "journal/12 Sep 26.md") == ""


@needs_git
def test_a_write_that_would_change_an_existing_line_is_refused(tmp_path, monkeypatch):
    v = make_vault(tmp_path)
    head = git("rev-parse", "main", cwd=v["remote"]).strip()

    def rewriting(existing, ap, new_note=""):
        return (existing or new_note).replace("the user wrote this", "Raphael rewrote this") + "x\n"

    monkeypatch.setattr(notes, "append_text", rewriting)
    with pytest.raises(notes.NotesError):
        notes.write_sync(v["cfg"], [_daily(date(2026, 9, 12))], "journal")
    assert git("rev-parse", "main", cwd=v["remote"]).strip() == head
    assert "the user wrote this" in remote_file(v, "journal/12 Sep 26.md")


@needs_git
def test_an_alternative_that_is_not_a_root_journal_note_is_refused(tmp_path):
    v = make_vault(tmp_path)
    ap = notes.Append(
        rel=notes.daily_note_path(date(2026, 9, 12)),
        key="daylog:2026-09-12",
        body="x",
        journal=True,
        alt_rel="knowledge/dev/secrets.md",
        section=("Journal",),
    )
    with pytest.raises(notes.NotesPathError):
        notes.write_sync(v["cfg"], [ap], "journal")


@needs_git
def test_rollups_read_the_filed_note_and_the_live_root_one(tmp_path):
    v = make_vault(tmp_path)  # seeds the live root note journal/12 Sep 26.md
    key = notes.journal_key("daily", "2026-09-11")
    filed = notes.daily_note_path(date(2026, 9, 11))
    # A day the user already filed, carrying Raphael's block in the current shape.
    device_commit(
        v,
        {
            filed: f"# Sep 11, 2026\n## Journal\n- mine\n- #raphael day log {notes.marker(key)}\n\t- Filed day.\n"
        },
    )
    days = notes.read_journal_days_sync(
        v["cfg"], [date(2026, 9, 11), date(2026, 9, 12), date(2026, 9, 13)]
    )
    assert set(days) == {"2026-09-11", "2026-09-12"}
    assert "the user wrote this" in days["2026-09-12"]
    mine, rest = notes.split_section(days["2026-09-11"], key)
    assert mine == "Filed day."
    assert "- mine" in rest


@needs_git
def test_an_entry_lands_mid_note_when_the_journal_section_is_not_last(tmp_path):
    user_note = "# Sep 12, 2026\n## Journal\n- mine\n---\n### Review \n- reviewed\n"
    v = make_vault(tmp_path, {"journal/12 Sep 26.md": user_note, ".obsidian/app.json": "{}\n"})
    notes.write_sync(v["cfg"], [_daily(date(2026, 9, 12))], "journal")
    text = remote_file(v, "journal/12 Sep 26.md")
    assert notes.is_one_insertion(user_note, text)
    assert text.index("- mine") < text.index("- #raphael day log") < text.index("---\n### Review")
