"""The record folder leaves the note index (vault record spec §5), and
NotesSyncFlow compiles the record behind a patch marker (Task 5)."""

from __future__ import annotations

from aegis.services import vault_layout as vl
from aegis_worker.activities.notes import unindexed_prefixes


def test_the_index_drops_the_record_folder_and_the_questions():
    assert unindexed_prefixes(vl.DEFAULT_LAYOUT) == ("raphael/questions/", "me/")
    lay = vl.layout_from({"record": {"dir": "about-me"}})
    assert unindexed_prefixes(lay) == ("raphael/questions/", "about-me/")
