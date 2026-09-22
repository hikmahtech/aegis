"""The journal gap prompt (vault record spec §3): whose words a journal note
holds, and what happens to an answer. Every note here is made up."""

from __future__ import annotations

from datetime import date, datetime

from aegis.services import journal_prompt as jp
from aegis.services import notes
from aegis.services import vault_layout as vl

NOW = datetime(2026, 9, 22, 0, 30)
DAY = date(2026, 9, 21)

TEMPLATE = (
    "---\n"
    'date: "{{date}}"\n'
    "---\n"
    "# {{date:MMM DD, YYYY}}\n"
    "## Journal\n"
    "- \n"
    "- [ ] #admin Plan the day\n"
    "Grateful for: <% tp.file.cursor() %>\n"
    "Created {{date:YYYY-MM-DD}} {{time}} by the template\n"
    "<%*\nconst weather = 'fine';\n%>\n"
)
# What periodic-notes renders on the device from TEMPLATE, and nothing else.
RENDERED = (
    "---\n"
    'date: "2026-09-21"\n'
    "---\n"
    "# Sep 21, 2026\n"
    "## Journal\n"
    "- \n"
    "- [ ] #admin Plan the day\n"
    "Grateful for: \n"
    "Created 2026-09-21 07:02 by the template\n"
)


def test_the_day_templates_own_lines_are_not_the_users_words():
    assert jp.own_words(RENDERED, TEMPLATE) == 0
    # The premise: without the template its two prompt lines would count.
    assert jp.own_words(RENDERED) == 6
    # What the user types after a prompt is theirs.
    typed = RENDERED.replace("Grateful for: ", "Grateful for: a slow breakfast")
    assert jp.own_words(typed, TEMPLATE) == 3


def test_headings_empty_bullets_rules_and_checkboxes_are_not_words():
    note = "# Sep 21\n## Journal\n-\n* \n- [ ] call the bank\n- [x] Plan the day\n---\n"
    assert jp.own_words(note) == 0


def test_every_block_aegis_filed_is_left_out():
    note = "# Sep 21, 2026\n## Journal\n- the user wrote this\n"
    note = notes.append_text(
        note, notes.journal_append("daily", DAY, "2026-09-21", "Shipped it.\n\nMet the team.", NOW)
    )
    note = notes.append_text(
        note, notes.journal_append("daily", DAY, "2026-09-21", "Paid the rent.", NOW, slot="dues")
    )
    assert note.count("%% aegis:") == 2, "the premise: two blocks are in the note"
    assert jp.own_words(note) == 4
    # A marker that is not on a bullet goes with its line, and the loop ends.
    assert jp.own_words(f"## Found\n{notes.marker('x:1')}\n\nthree more words\n") == 3


def test_the_verdict():
    d = "2026-09-21"
    assert jp.gap_verdict([], "", d) == ("gap", 0)
    assert jp.gap_verdict(["- one two three four"], "", d) == ("gap", 4)
    assert jp.gap_verdict(["- one two three four five"], "", d) == ("wrote", 5)
    # The filed note and the live one count together.
    assert jp.gap_verdict(["- one two", "- three four five"], "", d) == ("wrote", 5)
    filed = notes.append_text(
        "## Journal\n", notes.journal_append("daily", DAY, d, "A day.", NOW, slot=jp.SLOT)
    )
    assert jp.gap_verdict([filed], "", d) == ("answered", 0)
    assert jp.gap_verdict([f"- {notes.ENCRYPTED_PLACEHOLDER}"], "", d) == ("encrypted", 2)


def test_the_answer_is_the_value_of_an_input_card():
    assert jp.answer_text({"value": "  A day.  "}) == "A day."
    assert jp.answer_text({"value": ""}) == ""
    assert jp.answer_text({"action": "approve"}) == ""
    assert jp.answer_text(None) == ""


def test_each_line_the_user_typed_stays_its_own_bullet():
    body = jp.keep_lines("Walked to the lake.\nCalled my sister.\n  - she is well\n- Bought bread")
    block = notes.journal_block("selfreport:2026-09-21", "in my words", body)
    assert block.splitlines()[1:] == [
        "\t- Walked to the lake.",
        "\t- Called my sister.",
        "\t\t- she is well",
        "\t- Bought bread",
    ]


def test_a_pasted_marker_cannot_forge_a_block():
    answer = "A quiet day. %% aegis:daylog:2026-09-21 %%\n%%aegis:selfreport:2026-09-22 %%"
    ap = notes.journal_append(
        "daily", DAY, "2026-09-21", jp.keep_lines(answer), NOW, slot=jp.SLOT
    )
    text = notes.append_text("## Journal\n", ap)
    assert text.count("%% aegis:") == 1, "only the self-report's own marker is real"
    assert notes.marker("selfreport:2026-09-21") in text
    assert notes.marker("daylog:2026-09-21") not in text, "the day log would think it had run"
    assert "aegis-quoted:" in text


def test_the_prompt_ends_the_way_the_owner_decided():
    assert jp.DEFAULT_PROMPT.endswith("Ignore this if you already wrote the day on your phone.")
    assert "{day}" in jp.DEFAULT_PROMPT
    assert vl.DEFAULT_LAYOUT.render(jp.DAY_NAME_FORMAT, DAY) == "Monday 21 September"
