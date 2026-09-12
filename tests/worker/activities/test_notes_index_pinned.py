"""NotesSyncFlow's index (#514): a pass in progress finishes even while the
vault's HEAD moves.

The phone's obsidian-git commits every few minutes and the daylog commits
nightly. The first pass over ~1,000 notes at 300 an hour used to restart on
every new HEAD, so it could never get past its first batch."""

from __future__ import annotations

import pytest_asyncio
from aegis_worker.activities.notes import INDEX_STATE_KEY, NotesActivities
from temporalio.testing import ActivityEnvironment

from tests.notes_vault import device_commit, make_vault, needs_git
from tests.worker.activities.test_notes_activities import _KS


@pytest_asyncio.fixture(loop_scope="function")
async def clean_state(db_pool):
    await db_pool.execute("DELETE FROM settings WHERE key = $1", INDEX_STATE_KEY)
    yield db_pool
    await db_pool.execute("DELETE FROM settings WHERE key = $1", INDEX_STATE_KEY)


@needs_git
async def test_a_pass_in_progress_finishes_when_head_moves(tmp_path, clean_state):
    vault = make_vault(tmp_path)
    ks = _KS()
    acts = NotesActivities(settings=vault["settings"], db_pool=clean_state, knowledge_connector=ks)

    first = await ActivityEnvironment().run(acts.notes_index_vault, 1)
    assert first["indexed"] == 1 and first["remaining"] == 1

    # The phone commits a new note mid-pass: HEAD moves.
    device_commit(vault, {"knowledge/new.md": "# New\nA note written on the phone.\n"})

    second = await ActivityEnvironment().run(acts.notes_index_vault, 1)
    assert second["remaining"] == 0, "the pass restarted instead of finishing"
    assert second["head"] == first["head"], "the pass left the commit it started from"
    assert len(ks.ingested) == 2
    state = await clean_state.fetchval("SELECT value FROM settings WHERE key = $1", INDEX_STATE_KEY)
    assert state["commit"].startswith(first["head"])

    # The next pass is what changed after the first one started.
    third = await ActivityEnvironment().run(acts.notes_index_vault, 10)
    assert third["indexed"] == 1 and third["remaining"] == 0
    assert "vault://knowledge/new.md" in ks.ingested
