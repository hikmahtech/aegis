"""The writer under a layout that is not the shipped one (#514, the OSS audit).

A fork's vault files its journal differently: these tests take one such layout
end to end — the gate, the write, the block, reading it back — and pin the
rule for a layout that changes later: the old paths still count as written,
and nothing is ever moved.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from aegis.services import notes
from aegis.services import notes_write as nw
from aegis.services import vault_layout as vl

from tests.notes_vault import device_commit, git, make_vault, needs_git, remote_file

NOW = datetime(2026, 9, 12, 19, 5)

# A fork: an `assistant/` folder, ISO-dated notes under `diary/`, Sunday
# weeks, a different tag, four-space indents and its own section names.
CUSTOM = vl.layout_from(
    vl.validate(
        {
            "agent_dir": "assistant",
            "questions_dir": "assistant/answers",
            "week_start": "sunday",
            "week_numbering": "locale_us",
            "date_heading_format": "DD/MM/YYYY",
            "entry": {"tag": "#aegis", "indent": "four_spaces", "max_outline_depth": 3},
            "daily": {
                "folder": "[diary/]YYYY/MM",
                "format": "YYYY-MM-DD",
                "live_folder": "diary",
                "template": "_templates/day.md",
                "sections": ["Log"],
                "label": "diary",
            },
            "weekly": {
                "folder": "[diary/]YYYY",
                "format": "YYYY-[W]ww",
                "live_folder": "",
                "template": "",
                "sections": ["Week"],
                "label": "the week",
            },
            "monthly": {"folder": "[diary/]YYYY", "format": "YYYY-MM", "sections": ["Month"]},
        }
    )
)


def _daily(day: date, body: str = "The day.", layout: vl.Layout = CUSTOM) -> notes.Append:
    return notes.journal_append("daily", day, day.isoformat(), body, NOW, layout)


# ----------------------------------------------------------------- gate


def test_the_gate_follows_the_layout():
    assert notes.check_path("assistant/topics/rag.md", layout=CUSTOM)
    with pytest.raises(notes.NotesPathError):
        notes.check_path("raphael/topics/rag.md", layout=CUSTOM)
    assert notes.check_path("diary/2026/09/2026-09-12.md", journal=True, layout=CUSTOM)
    with pytest.raises(notes.NotesPathError):
        notes.check_path("journal/2026/09. Sep/12 Sep 26.md", journal=True, layout=CUSTOM)
    with pytest.raises(notes.NotesPathError):
        notes.check_path("diary/2026-09-12.md", journal=True, layout=CUSTOM), "live: never created"
    assert notes.is_journal_root_path("diary/2026-09-12.md", CUSTOM)
    # The safety refusals do not depend on the layout.
    for rel in ("assistant/../diary/x.md", "assistant/.hidden.md", "assistant/x.txt", "assistant"):
        with pytest.raises(notes.NotesPathError):
            notes.check_path(rel, journal=True, layout=CUSTOM)


def test_normalise_prefixes_the_layouts_folder_and_dates_the_heading():
    payload, problem = nw.normalise(
        "write", {"path": "topics/rag", "text": "t"}, now=datetime(2026, 9, 12), layout=CUSTOM
    )
    assert problem is None
    assert payload["path"] == "assistant/topics/rag.md" and payload["heading"] == "12/09/2026"
    # The shipped folder is just a folder inside the agent's own here.
    payload, _ = nw.normalise("write", {"path": "raphael/x", "text": "t"}, layout=CUSTOM)
    assert payload["path"] == "assistant/raphael/x.md"
    _, problem = nw.normalise("write", {"path": "../journal/x", "text": "t"}, layout=CUSTOM)
    assert problem


# ---------------------------------------------------------------- block


def test_the_block_uses_the_layouts_tag_indent_and_depth():
    block = notes.journal_block("daylog:2026-09-12", "diary", "Top:\n  - a\n    - b\n      - c", CUSTOM)
    assert block == (
        "- #aegis diary %% aegis:daylog:2026-09-12 %%\n"
        "    - Top:\n"
        "        - a\n"
        "            - b\n"
        "            - c\n"
    )
    mine, _ = notes.split_section(f"## Log\n{block}\n## Other\n", "daylog:2026-09-12", indent_width=4)
    assert mine == "Top:\n  - a\n    - b\n    - c"
    # Read with the wrong width the outline is misread — which is why the
    # reader takes the layout's indent.
    wrong, _ = notes.split_section(f"## Log\n{block}\n", "daylog:2026-09-12")
    assert wrong != mine


def test_an_empty_tag_and_label_still_make_a_well_formed_bullet():
    lay = vl.layout_from({"entry": {"tag": ""}, "daily": {"label": ""}})
    ap = notes.journal_append("daily", date(2026, 9, 12), "2026-09-12", "Text.", NOW, lay)
    assert notes.journal_block(ap.key, ap.label, ap.body, lay) == (
        "- %% aegis:daylog:2026-09-12 %%\n\t- Text.\n"
    )


def test_the_entry_goes_into_the_layouts_section():
    existing = "# 2026-09-12\n## Log\n- mine\n\n## Journal\n- not this one\n"
    out = notes.append_text(existing, _daily(date(2026, 9, 12)))
    assert notes.is_one_insertion(existing, out)
    assert out.index("- mine") < out.index("- #aegis diary") < out.index("## Journal")


def test_a_section_may_run_past_rules_and_fences_when_the_layout_says_so():
    lay = vl.layout_from({"section_ends_at_rule_or_fence": False})
    existing = "## Journal\n- mine\n---\n- still the journal\n## Next\n"
    out = notes.append_text(existing, _daily(date(2026, 9, 12), layout=lay))
    assert out.index("- still the journal") < out.index("- #aegis") < out.index("## Next")
    strict = notes.append_text(existing, _daily(date(2026, 9, 12), layout=vl.DEFAULT_LAYOUT))
    assert strict.index("- #aegis") < strict.index("---")


def test_a_new_note_keeps_its_open_tasks_when_the_layout_says_so(tmp_path):
    (tmp_path / "_templates").mkdir()
    (tmp_path / "_templates/day.md").write_text("# {{title}}\n## Log\n- \n- [ ] plan\n", "utf-8")
    cfg = notes.NotesConfig(path=tmp_path)
    keep = vl.layout_from(
        {
            **vl.layout_to_dict(CUSTOM),
            "new_note": {"drop_open_tasks": False, "drop_empty_bullets_in_section": False},
        }
    )
    ap = _daily(date(2026, 9, 12), layout=keep)
    text = notes.append_text(None, ap, notes._new_note_text(cfg, ap))
    assert text == (
        "# 2026-09-12\n## Log\n- \n- [ ] plan\n"
        "- #aegis diary %% aegis:daylog:2026-09-12 %%\n    - The day.\n"
    )
    ap = _daily(date(2026, 9, 12))
    text = notes.append_text(None, ap, notes._new_note_text(cfg, ap))
    assert "[ ]" not in text and "\n- \n" not in text


def test_a_kind_without_a_template_starts_with_its_title(tmp_path):
    cfg = notes.NotesConfig(path=tmp_path)
    ap = notes.journal_append("weekly", date(2026, 9, 9), "2026-W37", "Week.", NOW, CUSTOM)
    assert ap.rel == "diary/2026/2026-W37.md" and ap.alt_rel == ""
    assert notes._new_note_text(cfg, ap) == "# 2026-W37\n"


def test_question_notes_go_to_the_layouts_folder_and_keep_their_letters():
    ap = notes.question_append("¿Qué es RAG?", "answer", datetime(2026, 9, 12), CUSTOM)
    assert ap.rel.startswith("assistant/answers/qué-es-rag-") and ap.heading == "12/09/2026"
    ap = notes.question_append("什么是 RAG？", "answer", datetime(2026, 9, 12))
    assert ap.rel.startswith("raphael/questions/什么是-rag-")
    assert notes._slug("!!!") == "question"


# ------------------------------------------------------------ git trips

SEED = {
    "_templates/day.md": "# {{date:MMMM Do}}\n## Log\n- \n",
    "diary/2026-09-12.md": "# today\n## Log\n- the user wrote this\n",
    ".obsidian/app.json": "{}\n",
}


@needs_git
def test_a_custom_layout_end_to_end(tmp_path):
    v = make_vault(tmp_path, SEED)
    cfg = v["cfg"]
    # A filed day, rendered from the template, in the layout's folder.
    res = notes.write_sync(cfg, [_daily(date(2026, 9, 13))], "journal")
    assert res["outcomes"] == [{"path": "diary/2026/09/2026-09-13.md", "changed": True}]
    text = remote_file(v, "diary/2026/09/2026-09-13.md")
    assert text == (
        "# September 13th\n## Log\n"
        "- #aegis diary %% aegis:daylog:2026-09-13 %%\n    - The day.\n"
    )
    # The live note takes the day the user has open.
    res = notes.write_sync(cfg, [_daily(date(2026, 9, 12))], "journal")
    assert res["outcomes"] == [{"path": "diary/2026-09-12.md", "changed": True}]
    assert remote_file(v, "diary/2026/09/2026-09-12.md") == ""
    # The shipped layout's paths are not journal notes under this one.
    stray = notes.Append(
        rel="journal/2026/09. Sep/14 Sep 26.md", key="daylog:2026-09-14", body="x",
        journal=True, section=("Log",), layout=CUSTOM,
    )
    with pytest.raises(notes.NotesPathError):
        notes.write_sync(cfg, [stray], "journal")
    # Reading back: both days, the block first, with the four-space indent.
    days = notes.read_journal_days_sync(cfg, [date(2026, 9, 12), date(2026, 9, 13)], CUSTOM)
    assert set(days) == {"2026-09-12", "2026-09-13"}
    mine, rest = notes.split_section(
        days["2026-09-13"], notes.journal_key("daily", "2026-09-13"), indent_width=4
    )
    assert mine == "The day." and rest == "# September 13th\n## Log"


@needs_git
def test_a_changed_layout_never_writes_a_day_twice_and_moves_nothing(tmp_path):
    """The rule for a layout that changes: the day written under the old
    layout stays where it is, and a re-run or a backfill finds its marker
    there instead of writing it again under the new paths."""
    v = make_vault(tmp_path)
    cfg = v["cfg"]
    old = vl.DEFAULT_LAYOUT
    notes.write_sync(cfg, [_daily(date(2026, 9, 12), layout=old)], "journal")  # the live root note
    notes.write_sync(cfg, [_daily(date(2026, 9, 13), layout=old)], "journal")  # a filed note
    head = git("rev-parse", "main", cwd=v["remote"]).strip()

    new = vl.layout_from({**vl.layout_to_dict(CUSTOM), "previous": vl.layout_to_dict(old)})
    assert new.previous == old
    ap = _daily(date(2026, 9, 13), "a retried body", layout=new)
    assert ap.rel == "diary/2026/09/2026-09-13.md"
    assert ap.also_rels == ("journal/2026/09. Sep/13 Sep 26.md", "journal/13 Sep 26.md")
    again = notes.write_sync(cfg, [ap, _daily(date(2026, 9, 12), layout=new)], "journal")
    assert again["status"] == "exists"
    assert again["outcomes"] == [
        {"path": "journal/2026/09. Sep/13 Sep 26.md", "changed": False},
        {"path": "journal/12 Sep 26.md", "changed": False},
    ]
    assert git("rev-parse", "main", cwd=v["remote"]).strip() == head
    assert remote_file(v, "diary/2026/09/2026-09-13.md") == ""
    # The rollup reader still finds the old days through `previous`.
    days = notes.read_journal_days_sync(cfg, [date(2026, 9, 12), date(2026, 9, 13)], new)
    assert set(days) == {"2026-09-12", "2026-09-13"}
    # A day the old layout never wrote goes to the new path.
    fresh = notes.write_sync(cfg, [_daily(date(2026, 9, 14), layout=new)], "journal")
    assert fresh["outcomes"] == [{"path": "diary/2026/09/2026-09-14.md", "changed": True}]
    # Without `previous` the same day would be written again — which is why
    # the PUT keeps it.
    bare = notes.write_sync(cfg, [_daily(date(2026, 9, 13), "twice", layout=CUSTOM)], "journal")
    assert bare["outcomes"] == [{"path": "diary/2026/09/2026-09-13.md", "changed": True}]


@needs_git
def test_the_commit_is_signed_by_the_owning_agent(tmp_path):
    v = make_vault(tmp_path)
    author = notes.author_for("helper", "Helper Bot")
    assert author == notes.Author(name="Helper Bot", email="helper@aegis.local", prefix="helper")
    notes.write_sync(v["cfg"], [_daily(date(2026, 9, 13), layout=vl.DEFAULT_LAYOUT)], "", author=author)
    log = git("log", "-1", "--format=%an <%ae>%n%s", "main", cwd=v["remote"])
    assert log == "Helper Bot <helper@aegis.local>\nhelper: notes\n"
    assert notes.author_for("") == notes.author_for(None) == notes.DEFAULT_AUTHOR
    assert notes.author_for("Ra ph", None).prefix == "ra-ph"


@needs_git
def test_the_index_follows_the_layouts_skip_list(tmp_path):
    v = make_vault(
        tmp_path,
        {"drafts/x.md": "# x\n", "notes/y.md": "# y\n", "raphael/questions/q.md": "# q\n"},
    )
    lay = vl.layout_from({"index_skip_prefixes": ["drafts/"]})
    changes = notes.vault_changes_sync(v["cfg"], None, lay)
    assert changes.changed == ["notes/y.md", "raphael/questions/q.md"]
    device_commit(v, {"drafts/x.md": "# changed\n", "notes/z.md": "# z\n"})
    later = notes.vault_changes_sync(v["cfg"], changes.head, lay)
    assert later.changed == ["notes/z.md"]
