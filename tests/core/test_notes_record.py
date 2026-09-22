"""The record folder in the vault (vault record spec §4, §12): read it, write a
draft once, and list note titles with their tags for the interests seed.
Local bare repos, as test_notes.py; every note is made up here."""

from __future__ import annotations

import pytest
from aegis.services import notes
from aegis.services import vault_layout as vl

from tests.notes_vault import CIPHER, device_commit, make_vault, needs_git, remote_file, remote_head

LAYOUT = vl.DEFAULT_LAYOUT


def _draft(name: str, text: str) -> notes.Append:
    return notes.Append(
        rel=LAYOUT.record.draft_path(name), key=f"record-seed:{name}", body=text,
        record=True, create_only=True, layout=LAYOUT,
    )


def test_the_gate_allows_a_record_path_only_when_asked():
    assert notes.check_path("me/about.draft.md", record=True) == "me/about.draft.md"
    for rel in ("me/about.draft.md", "me/about.md"):
        with pytest.raises(notes.NotesPathError):
            notes.check_path(rel)
    with pytest.raises(notes.NotesPathError):
        notes.check_path("me/sub/about.draft.md", record=True)


def test_note_tags_reads_frontmatter_and_inline_tags():
    assert notes.note_tags("---\ntags: [bread, Fermentation]\n---\n# Sourdough\nfed the #starter\n") == (
        "bread", "fermentation", "starter",
    )
    assert notes.note_tags("---\ntags:\n  - books\n  - '#slow'\n---\nbody\n") == ("books", "slow")
    assert notes.note_tags("# Heading\nsee https://example.com/page#anchor and #1\n") == ()
    assert notes.note_tags(f"{CIPHER} #visible") == ("visible",)


@needs_git
async def test_a_draft_is_written_once_and_never_again(tmp_path):
    v = make_vault(tmp_path, {"knowledge/a.md": "# A\n"})
    first = await notes.write(v["cfg"], [_draft("about", "%% Draft %%\n# About\n\n## Notes\n- Lives in Exampletown")], "t")
    assert first["status"] == "written" and first["changed"] == ["me/about.draft.md"]
    assert remote_file(v, "me/about.draft.md") == "%% Draft %%\n# About\n\n## Notes\n- Lives in Exampletown\n"
    head = remote_head(v)
    again = await notes.write(v["cfg"], [_draft("about", "something else")], "t")
    assert again["status"] == "exists" and remote_head(v) == head


@needs_git
async def test_only_a_create_only_draft_may_go_in_the_record_folder(tmp_path):
    v = make_vault(tmp_path, {"knowledge/a.md": "# A\n"})
    appended = notes.Append(rel="me/about.draft.md", key="x", body="b", record=True, layout=LAYOUT)
    accepted = notes.Append(rel="me/about.md", key="x", body="b", record=True, create_only=True, layout=LAYOUT)
    for ap in (appended, accepted):
        with pytest.raises(notes.NotesPathError):
            await notes.write(v["cfg"], [ap], "t")


@needs_git
async def test_read_record_lists_notes_and_drafts_and_strips_ciphertext(tmp_path):
    v = make_vault(tmp_path, {"knowledge/a.md": "# A\n"})
    missing = notes.read_record_sync(v["cfg"], LAYOUT)
    assert missing.missing and missing.notes == {} and missing.head
    device_commit(v, {
        "me/about.md": f"# About\n- Lives in Exampletown\n{CIPHER}\n",
        "me/work.draft.md": "# Work\n",
        "me/sub/deep.md": "# Deep\n",
    })
    files = notes.read_record_sync(v["cfg"], LAYOUT)
    assert not files.missing
    assert set(files.notes) == {"about"} and files.drafts == ("work",)
    assert "c2VjcmV0" not in files.notes["about"] and notes.ENCRYPTED_PLACEHOLDER in files.notes["about"]


@needs_git
def test_the_catalogue_leaves_out_the_journal_the_record_and_the_agents_folder(tmp_path):
    v = make_vault(tmp_path, {
        "knowledge/cooking/sourdough.md": "---\ntags: [bread]\n---\n# Sourdough\n#starter\n",
        "reading.md": "---\ntags:\n  - books\n---\n",
        "journal/2026/09. Sep/12 Sep 26.md": "# day #private\n",
        "journal/12 Sep 26.md": "# day\n",
        "me/about.md": "# About\n",
        "raphael/questions/q.md": "# Q\n",
        "_templates/day.md": "# T\n",
    })
    assert notes.catalogue_sync(v["cfg"], LAYOUT) == [
        ("knowledge/cooking/sourdough.md", ("bread", "starter")),
        ("reading.md", ("books",)),
    ]
