"""Vault details from the programme's validation (#514): the later
meld-encrypt markers, a journal note's own section, `Do`, and the weekly
template's date."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from aegis.services import notes

P = notes.ENCRYPTED_PLACEHOLDER


def test_strip_encrypted_removes_the_bare_forms_later_versions_write():
    text = "keep 🔐β c2VjcmV0 🔐 and 🔐α bW9yZQ== 🔐 end"
    out = notes.strip_encrypted(text)
    assert "c2VjcmV0" not in out and "bW9yZQ" not in out
    assert out == f"keep {P} and {P} end"


def test_strip_encrypted_still_removes_the_percent_forms():
    assert notes.strip_encrypted("a %%🔐α c2VjcmV0 🔐%% b") == f"a {P} b"
    assert notes.strip_encrypted("a %%🔐β c2VjcmV0 🔐%% b") == f"a {P} b"


def test_an_ordinary_lock_emoji_is_not_a_block():
    text = "## 🔐 Passwords\nuse the manager 🔐 always"
    assert notes.strip_encrypted(text) == text


def test_an_unterminated_bare_block_drops_the_rest():
    assert notes.strip_encrypted("a 🔐β dGFpbA== and more") == f"a {P}"


def test_split_section_takes_raphaels_section_and_leaves_the_rest():
    key = notes.journal_key("daily", "2026-09-12")
    text = (
        "# 12 Sep 26\nmy long day\n"
        f"\n## Raphael\n{notes.marker(key)}\n\nThe narrative.\n"
        "\n## Later\nmore of mine\n"
    )
    mine, rest = notes.split_section(text, key)
    assert mine == "The narrative."
    assert "my long day" in rest and "more of mine" in rest
    assert "narrative" not in rest and "## Raphael" not in rest


def test_split_section_without_the_marker_is_the_whole_note():
    assert notes.split_section("just mine", "daylog:2026-09-12") == ("", "just mine")


@pytest.mark.parametrize(
    ("day", "want"),
    [(1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"), (11, "11th"), (12, "12th"),
     (13, "13th"), (21, "21st"), (22, "22nd"), (23, "23rd"), (31, "31st")],
)
def test_do_is_an_ordinal_day(day, want):
    assert notes.moment_format("Do", date(2026, 1, day)) == want


def test_do_inside_a_longer_format():
    assert notes.moment_format("dddd, MMMM Do YYYY", date(2026, 9, 1)) == "Tuesday, September 1st 2026"


def test_a_weekly_note_renders_its_template_for_the_weeks_monday():
    """The vault names and dates a week from its Monday (`W40 Oct 23` opens
    `# Oct 02, 2023`), which is also the ISO Monday the daylog passes in."""
    ap = notes.journal_append("weekly", date(2026, 9, 9), "2026-W37", "body", datetime(2026, 9, 13, 21, 0))
    assert ap.rel == "journal/2026/09. Sep/W37 Sep 26.md"
    assert ap.when is not None and ap.when.date() == date(2026, 9, 7)
