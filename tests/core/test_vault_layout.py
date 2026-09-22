"""The vault layout row (`services/vault_layout.py`).

The shipped defaults must be exactly what the code did before the row existed
(the literal paths in `test_notes.py` are the other half of that proof), the
read path must never raise, the write path must refuse every bad key, and a
changed layout must keep the one before it.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

import pytest
import pytest_asyncio
from aegis.services import notes
from aegis.services import vault_layout as vl

# ------------------------------------------------------------- defaults


def test_the_defaults_are_todays_layout():
    lay = vl.DEFAULT_LAYOUT
    assert lay == vl.layout_from({}) == vl.layout_from(None) == vl.layout_from("junk")
    assert vl.layout_to_dict(lay) == vl.merge({})
    assert lay.agent_dir == "raphael" and lay.questions_dir == "raphael/questions"
    assert lay.entry_tag == "#aegis/{agent}" and lay.indent_text == "\t"
    assert lay.note_path("daily", date(2023, 10, 24)) == "journal/2023/10. Oct/24 Oct 23.md"
    assert lay.note_path("weekly", date(2023, 10, 5)) == "journal/2023/10. Oct/W40 Oct 23.md"
    assert lay.note_path("monthly", date(2023, 8, 1)) == "journal/2023/08. Aug/08. Aug.md"
    assert lay.root_path("daily", date(2023, 10, 25)) == "journal/25 Oct 23.md"
    assert lay.root_path("weekly", date(2023, 10, 25)) == "journal/W43 Oct 23.md"
    assert lay.root_path("monthly", date(2023, 10, 25)) == ""
    assert lay.daily.sections == ("Journal",) and lay.monthly.sections == ("Review", "Month Review")
    assert lay.previous is None


def test_the_shipped_week_rule_is_the_iso_week():
    d = date(2000, 1, 1)
    while d < date(2035, 1, 1):
        assert vl.week_of(d) == d.isocalendar()[:2], d
        assert vl.week_start_of(d) == d - timedelta(days=d.weekday()), d
        d += timedelta(days=1)


def test_sunday_weeks_and_us_numbering():
    # moment `en`: weeks start Sunday, week 1 holds January 1st.
    assert vl.week_start_of(date(2026, 9, 12), "sunday") == date(2026, 9, 6)
    assert vl.week_of(date(2026, 1, 1), "sunday", "locale_us") == (2026, 1)
    assert vl.week_of(date(2025, 12, 28), "sunday", "locale_us") == (2026, 1)  # the same week
    assert vl.week_of(date(2025, 12, 27), "sunday", "locale_us") == (2025, 52)
    start, end, label = vl.week_bounds(date(2026, 9, 9), "sunday", "locale_us")
    assert (start, end, label) == (date(2026, 9, 6), date(2026, 9, 12), "2026-W37")
    # Sunday-first with ISO numbering (moment's dow 0, doy 4): week 1 is the
    # week holding January 3rd, so 2026 has 53 of them and Sat 2 Jan 2027 is
    # still in its last.
    assert vl.week_of(date(2027, 1, 3), "sunday", "iso") == (2027, 1)
    assert vl.week_of(date(2027, 1, 2), "sunday", "iso") == (2026, 53)
    assert vl.week_of(date(2026, 1, 1), "sunday", "iso") == (2026, 1)


def test_moment_format_uses_the_locale_table_and_the_week_rule():
    assert vl.moment_format("dddd Do MMMM", date(2026, 9, 12)) == "Saturday 12th September"
    assert vl.moment_format("[W]ww", date(2026, 9, 6)) == "W36"
    assert vl.moment_format("[W]ww", date(2026, 9, 6), week_start="sunday", week_numbering="locale_us") == "W37"
    assert vl.format_tokens("[journal/]YYYY/MM[. ]MMM") == {"YYYY", "MM", "MMM"}


# ---------------------------------------------------------------- paths


def test_the_journal_patterns_are_generated_from_the_layout():
    lay = vl.layout_from(
        {
            "daily": {"folder": "[notes/]YYYY/MM", "format": "YYYY-MM-DD", "live_folder": ""},
            "weekly": {"folder": "[notes/]YYYY", "format": "YYYY-[W]ww", "live_folder": "notes/live"},
            "monthly": {"folder": "[notes/]YYYY", "format": "YYYY-MM", "live_folder": ""},
        }
    )
    assert lay.note_path("daily", date(2026, 9, 12)) == "notes/2026/09/2026-09-12.md"
    assert lay.note_path("weekly", date(2026, 9, 12)) == "notes/2026/2026-W37.md"
    assert lay.root_path("weekly", date(2026, 9, 12)) == "notes/live/2026-W37.md"
    assert lay.note_path("monthly", date(2026, 9, 12)) == "notes/2026/2026-09.md"
    assert lay.is_journal_path("notes/2026/09/2026-09-12.md")
    assert lay.is_journal_path("notes/2026/2026-W37.md")
    assert lay.is_journal_path("notes/2026/2026-09.md")
    assert lay.is_journal_root_path("notes/live/2026-W37.md")
    # A token rendered twice must agree with itself: the folder's year and
    # month against the name's.
    assert not lay.is_journal_path("notes/2026/09/2025-09-12.md")
    assert not lay.is_journal_path("notes/2026/08/2026-09-12.md")
    # The shipped layout's paths are not this layout's, and vice versa.
    assert not lay.is_journal_path("journal/2026/09. Sep/12 Sep 26.md")
    assert not vl.DEFAULT_LAYOUT.is_journal_path("notes/2026/09/2026-09-12.md")
    # A literal that is a regex metacharacter is escaped, and month names
    # come from the locale: `Sep` matches, `Zzz` does not.
    assert vl.DEFAULT_LAYOUT.is_journal_path("journal/2026/09. Sep/12 Sep 26.md")
    assert not vl.DEFAULT_LAYOUT.is_journal_path("journal/2026/09. Zzz/12 Zzz 26.md")
    assert not vl.DEFAULT_LAYOUT.is_journal_path("journal/2026/09X Sep/12 Sep 26.md")


def test_a_disabled_kind_has_no_pattern_and_no_append():
    lay = vl.layout_from({"weekly": {"enabled": False}})
    assert not lay.is_journal_path("journal/2026/09. Sep/W37 Sep 26.md")
    assert lay.is_journal_path("journal/2026/09. Sep/12 Sep 26.md")
    with pytest.raises(notes.JournalKindDisabled):
        notes.journal_append("weekly", date(2026, 9, 7), "2026-W37", "x", _NOW, lay)


_NOW = __import__("datetime").datetime(2026, 9, 12, 19, 5)


def test_the_label_of_a_block_comes_from_its_slot():
    layout = vl.layout_from({})
    assert layout.label_for("", "daily") == "day log"
    assert layout.label_for("", "weekly") == "week in review"
    assert layout.label_for("review", "weekly") == "weekly review"
    # A slot the wording does not name yet says its own name rather than
    # nothing, so no block is ever written with a bare tag.
    assert layout.label_for("dues", "weekly") == "dues"
    # It is configuration: the row wins, and an unknown wording key is refused.
    assert vl.layout_from({"language": {"review_label": "the week"}}).label_for(
        "review", "weekly"
    ) == "the week"
    with pytest.raises(ValueError, match="language.reviewlabel"):
        vl.validate({"language": {"reviewlabel": "x"}})


def test_a_self_report_is_labelled_by_its_own_wording():
    assert vl.layout_from({}).label_for("selfreport", "daily") == "in my words"


# ---------------------------------------------------------------- merge


def test_merge_reads_a_bad_key_as_its_default_and_never_raises():
    out = vl.merge(
        {
            "agent_dir": 7,
            "locale": "xx",
            "week_start": "friday",
            "entry": {"indent": "tabs", "max_outline_depth": "4", "tag": "#me"},
            "daily": {"sections": "Journal", "label": "diary"},
            "language": {"tasks": None, "quiet_day": "Nothing."},
            "index_skip_prefixes": "nope",
            "previous": {"agent_dir": "old"},
        }
    )
    assert out["agent_dir"] == "raphael" and out["locale"] == "en" and out["week_start"] == "monday"
    assert out["entry"] == {"tag": "#me", "indent": "tab", "max_outline_depth": 4}
    assert out["daily"]["sections"] == ["Journal"] and out["daily"]["label"] == "diary"
    assert out["language"]["tasks"] == "Completed:" and out["language"]["quiet_day"] == "Nothing."
    assert out["index_skip_prefixes"] == vl.DEFAULTS["index_skip_prefixes"]
    assert out["previous"]["agent_dir"] == "old" and "previous" not in out["previous"]
    assert vl.layout_from(out).previous.agent_dir == "old"


# ------------------------------------------------------------- validate


@pytest.mark.parametrize(
    ("bad", "key"),
    [
        ({"agent_dir": "a/b"}, "agent_dir"),
        ({"agent_dir": ".hidden"}, "agent_dir"),
        ({"agent_dir": ""}, "agent_dir"),
        ({"questions_dir": "elsewhere/q"}, "questions_dir"),
        ({"questions_dir": "raphael/../q"}, "questions_dir"),
        ({"locale": "xx"}, "locale"),
        ({"week_start": "friday"}, "week_start"),
        ({"week_numbering": "odd"}, "week_numbering"),
        ({"date_heading_format": ""}, "date_heading_format"),
        ({"date_heading_format": "[]"}, "date_heading_format"),
        ({"index_skip_prefixes": ["/abs/"]}, "index_skip_prefixes"),
        ({"index_skip_prefixes": "nope"}, "index_skip_prefixes"),
        ({"entry": {"tag": "raphael"}}, "entry.tag"),
        ({"entry": {"tag": "#two words"}}, "entry.tag"),
        ({"entry": {"indent": "tabs"}}, "entry.indent"),
        ({"entry": {"max_outline_depth": 0}}, "entry.max_outline_depth"),
        ({"entry": {"other": 1}}, "entry.other"),
        ({"new_note": {"drop_open_tasks": "yes"}}, "new_note.drop_open_tasks"),
        ({"section_ends_at_rule_or_fence": "yes"}, "section_ends_at_rule_or_fence"),
        ({"language": {"tasks": ""}}, "language.tasks"),
        ({"language": {"colour": "x"}}, "language.colour"),
        ({"daily": {"format": "MMM YY"}}, "daily.format"),
        ({"daily": {"format": "DD/MM/YY"}}, "daily.format"),
        ({"daily": {"format": "[]"}}, "daily.format"),
        ({"daily": {"folder": "/journal"}}, "daily.folder"),
        ({"daily": {"folder": "[../journal]"}}, "daily.folder"),
        ({"daily": {"folder": "[.journal]"}}, "daily.folder"),
        ({"daily": {"live_folder": "../x"}}, "daily.live_folder"),
        ({"daily": {"template": ".obsidian/t.md"}}, "daily.template"),
        ({"daily": {"template": "_templates/t.txt"}}, "daily.template"),
        ({"daily": {"sections": []}}, "daily.sections"),
        ({"daily": {"sections": ["Journal", " "]}}, "daily.sections"),
        ({"daily": {"sections": "Journal"}}, "daily.sections"),
        ({"daily": {"enabled": "true"}}, "daily.enabled"),
        ({"daily": {"extra": 1}}, "daily.extra"),
        ({"weekly": {"format": "MMM YY"}}, "weekly.format"),
        ({"monthly": {"format": "DD MMM YY"}}, "monthly.format"),
        ({"nope": 1}, "nope"),
    ],
)
def test_validate_names_the_first_bad_key(bad, key):
    with pytest.raises(ValueError, match=rf"^{key}"):
        vl.validate(bad)


def test_validate_refuses_a_row_that_is_not_an_object():
    with pytest.raises(ValueError):
        vl.validate("junk")


def test_validate_accepts_the_defaults_and_a_full_alternative():
    assert vl.validate({}) == vl.merge({})
    assert vl.validate(None) == vl.merge({})
    alt = {
        "agent_dir": "assistant",
        "questions_dir": "assistant/answers",
        "week_start": "sunday",
        "week_numbering": "locale_us",
        "date_heading_format": "dddd, MMMM Do YYYY",
        "entry": {"tag": "", "indent": "four_spaces", "max_outline_depth": 2},
        "daily": {"folder": "[diary/]YYYY", "format": "YYYY-MM-DD", "live_folder": "", "template": "", "sections": ["Log"], "label": ""},
        "monthly": {"format": "YYYY-MM"},
    }
    out = vl.validate(alt)
    assert out["agent_dir"] == "assistant" and out["entry"]["indent"] == "four_spaces"
    lay = vl.layout_from(out)
    assert lay.note_path("daily", date(2026, 9, 12)) == "diary/2026/2026-09-12.md"
    assert lay.root_path("daily", date(2026, 9, 12)) == ""
    assert lay.indent_text == "    " and lay.indent_width == 4


# -------------------------------------------------------------- preview


def test_preview_renders_every_kind_for_the_date():
    out = vl.preview(vl.DEFAULT_LAYOUT, date(2026, 9, 12))
    assert out["week"] == {"start": "2026-09-07", "end": "2026-09-13", "label": "2026-W37"}
    assert out["heading"] == "2026-09-12"
    assert out["daily"]["path"] == "journal/2026/09. Sep/12 Sep 26.md"
    assert out["daily"]["live_path"] == "journal/12 Sep 26.md"
    assert out["weekly"]["path"] == "journal/2026/09. Sep/W37 Sep 26.md"
    assert out["monthly"]["path"] == "journal/2026/09. Sep/09. Sep.md"
    assert out["monthly"]["live_path"] == ""


# -------------------------------------------------------------- storage


@pytest_asyncio.fixture(loop_scope="function")
async def layout_pool(db_pool):
    await db_pool.execute("DELETE FROM settings WHERE key = $1", vl.SETTINGS_KEY)
    vl.invalidate_cache()
    yield db_pool
    await db_pool.execute("DELETE FROM settings WHERE key = $1", vl.SETTINGS_KEY)
    vl.invalidate_cache()


async def test_get_layout_without_a_pool_or_a_row_is_the_default(layout_pool):
    assert await vl.get_layout(None) is vl.DEFAULT_LAYOUT
    assert await vl.get_layout(layout_pool) == vl.DEFAULT_LAYOUT


async def test_save_keeps_the_layout_before_a_change_as_previous(layout_pool):
    first = await vl.save_layout(layout_pool, {"agent_dir": "assistant", "questions_dir": "assistant/q"})
    assert first["agent_dir"] == "assistant"
    assert first["previous"]["agent_dir"] == "raphael" and "previous" not in first["previous"]
    # Saving the same layout again changes nothing, `previous` included.
    same = await vl.save_layout(layout_pool, {k: v for k, v in first.items() if k != "previous"})
    assert same["previous"]["agent_dir"] == "raphael"
    # A second change moves `previous` one step: the layout before THIS one.
    second = await vl.save_layout(layout_pool, {"agent_dir": "helper", "questions_dir": "helper/q"})
    assert second["previous"]["agent_dir"] == "assistant"
    lay = await vl.get_layout(layout_pool)
    assert lay.agent_dir == "helper" and lay.previous.agent_dir == "assistant"
    assert lay.previous.previous is None


async def test_save_validates_and_writes_nothing_on_a_bad_key(layout_pool):
    with pytest.raises(ValueError, match="^agent_dir"):
        await vl.save_layout(layout_pool, {"agent_dir": "a/b"})
    assert await vl.get_layout_value(layout_pool) == vl.merge({})


async def test_the_cache_is_invalidated_by_a_save(layout_pool):
    assert (await vl.get_layout(layout_pool)).agent_dir == "raphael"
    await vl.save_layout(layout_pool, {"agent_dir": "assistant", "questions_dir": "assistant/q"})
    assert (await vl.get_layout(layout_pool)).agent_dir == "assistant"
    # A hand edit of the row is not seen until the cache expires…
    await layout_pool.execute(
        "UPDATE settings SET value = $2 WHERE key = $1", vl.SETTINGS_KEY, {"agent_dir": "other"}
    )
    assert (await vl.get_layout(layout_pool)).agent_dir == "assistant"
    vl.invalidate_cache()
    assert (await vl.get_layout(layout_pool)).agent_dir == "other"


async def test_a_broken_row_reads_as_the_defaults(layout_pool):
    await layout_pool.execute(
        "INSERT INTO settings (key, value) VALUES ($1, $2)", vl.SETTINGS_KEY, "not an object"
    )
    assert await vl.get_layout(layout_pool) == vl.DEFAULT_LAYOUT


# ------------------------------------------------------------- the record


def test_the_record_block_is_off_and_names_nobody():
    rec = vl.DEFAULT_LAYOUT.record
    assert (rec.enabled, rec.dir, rec.shared, rec.by_tag, rec.max_chars) == (
        False, "me", ("about",), (), 6000,
    )
    assert vl.merge({})["record"] == {
        "enabled": False, "dir": "me", "shared": ["about"], "by_tag": {}, "max_chars": 6000,
    }
    assert vl.layout_to_dict(vl.DEFAULT_LAYOUT)["record"] == vl.merge({})["record"]


def test_merge_reads_a_bad_record_key_as_its_default():
    m = vl.merge({"record": {"enabled": "yes", "by_tag": {"finance": "money"}, "max_chars": 10}})
    assert m["record"]["enabled"] is False
    assert m["record"]["by_tag"] == {}
    assert m["record"]["max_chars"] == 6000
    assert vl.merge({"record": "junk"})["record"] == vl.merge({})["record"]


@pytest.mark.parametrize(
    "bad,key",
    [
        ({"record": {"colour": 1}}, "record.colour"),
        ({"record": {"enabled": "yes"}}, "record.enabled"),
        ({"record": {"by_tag": {"chef": ["money"]}}}, "record.by_tag.chef"),
        ({"record": {"by_tag": {"finance": "money"}}}, "record.by_tag.finance"),
        ({"record": {"dir": "a/b"}}, "record.dir"),
        ({"record": {"dir": "raphael"}}, "record.dir"),
        ({"record": {"dir": "journal"}}, "record.dir"),
        ({"record": {"shared": ["about.md"]}}, "record note name"),
        ({"record": {"shared": ["about.draft"]}}, "record note name"),
        ({"record": {"max_chars": 100}}, "record.max_chars"),
    ],
)
def test_validate_refuses_a_bad_record_block(bad, key):
    with pytest.raises(ValueError, match=f"^{re.escape(key)}"):
        vl.validate(bad)


def test_a_full_record_block_round_trips():
    row = {
        "record": {
            "enabled": True, "dir": "me", "shared": ["about"],
            "by_tag": {"finance": ["money"], "gtd": ["work", "people"]}, "max_chars": 4000,
        }
    }
    lay = vl.layout_from(vl.validate(row))
    assert lay.record.names_for("finance") == ("money",)
    assert lay.record.names_for("infra") == ()
    assert lay.record.claimed() == {"about", "money", "work", "people"}
    assert lay.record.draft_path("money") == "me/money.draft.md"
    assert lay.record.is_record_path("me/money.md") and not lay.record.is_record_path("me/a/b.md")
    assert vl.layout_to_dict(lay)["record"] == vl.validate(row)["record"]


def test_the_record_folder_is_never_indexed():
    lay = vl.DEFAULT_LAYOUT
    assert not lay.is_indexable("me/about.md")
    assert not lay.is_indexable("me/about.draft.md")
    assert lay.is_indexable("knowledge/me/about.md")
    assert lay.is_indexable("meals.md")


def test_the_journal_area_comes_from_the_layout():
    lay = vl.DEFAULT_LAYOUT
    assert lay.journal_roots() == ("journal",)
    assert lay.is_journal_area("journal/2026/09. Sep/12 Sep 26.md")
    assert lay.is_journal_area("journal/anything.md")
    assert not lay.is_journal_area("knowledge/journal.md")
    moved = vl.layout_from({"daily": {"folder": "[diary/]YYYY", "live_folder": ""}})
    assert "diary" in moved.journal_roots()


async def test_changing_only_the_record_keeps_previous(layout_pool):
    first = await vl.save_layout(layout_pool, {"agent_dir": "assistant", "questions_dir": "assistant/q"})
    body = {k: v for k, v in first.items() if k != "previous"}
    body["record"] = {**body["record"], "enabled": True}
    again = await vl.save_layout(layout_pool, body)
    assert again["record"]["enabled"] is True
    assert again["previous"]["agent_dir"] == "raphael", "a record-only change must not rotate previous"
