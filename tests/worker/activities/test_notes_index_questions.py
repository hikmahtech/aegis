"""The vault index leaves out `raphael/questions/` (#514, from the audit).

ResearchFlow keeps each answer twice: in the knowledge store under
`aegis://research/<hash>` and in the vault under `raphael/questions/`. Indexing
the note as well put every answer in retrieval twice. The index skips those
notes, and drops any row an earlier run made for one."""

from __future__ import annotations

import pytest_asyncio
from aegis.services import notes
from aegis.services.knowledge import _content_id_for
from aegis_worker.activities.notes import INDEX_STATE_KEY, NotesActivities
from temporalio.testing import ActivityEnvironment

from tests.notes_vault import SEED_FILES, make_vault, needs_git

QUESTION = "raphael/questions/why-is-the-sky-blue-1a2b3c4d.md"


class _KS:
    def __init__(self):
        self.ingested: dict[str, dict] = {}
        self.deleted: list[str] = []

    async def ingest_content(self, **kwargs):
        self.ingested[kwargs["url"]] = kwargs
        return {"status": "ok", "content_id": _content_id_for(kwargs["url"])}

    async def delete_content(self, content_id):
        self.deleted.append(content_id)
        return True


@pytest_asyncio.fixture(loop_scope="function")
async def stale_row(db_pool):
    """The row an earlier run put in the index for the question note."""
    url = notes.note_url(QUESTION)
    cid = _content_id_for(url)
    await db_pool.execute("DELETE FROM settings WHERE key = $1", INDEX_STATE_KEY)
    await db_pool.execute(
        "INSERT INTO knowledge_content (content_id, url, title, source_type) "
        "VALUES ($1, $2, 'Why is the sky blue', 'note') ON CONFLICT (content_id) DO NOTHING",
        cid,
        url,
    )
    yield db_pool, url, cid
    await db_pool.execute("DELETE FROM knowledge_content WHERE content_id = $1", cid)
    await db_pool.execute("DELETE FROM settings WHERE key = $1", INDEX_STATE_KEY)


@needs_git
async def test_research_answers_are_not_indexed_and_an_old_row_is_dropped(tmp_path, stale_row):
    pool, url, cid = stale_row
    vault = make_vault(tmp_path, {**SEED_FILES, QUESTION: "# Why is the sky blue\n\nRayleigh.\n"})
    ks = _KS()
    acts = NotesActivities(settings=vault["settings"], db_pool=pool, knowledge_connector=ks)
    out = await ActivityEnvironment().run(acts.notes_index_vault, 50)
    assert out["status"] == "ok"
    assert url not in ks.ingested
    assert cid in ks.deleted
    assert "vault://journal/12 Sep 26.md" in ks.ingested, "every other note is still indexed"
