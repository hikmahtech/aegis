"""NotesActivities (#514): the journal write, the vault index and the backfill,
against a throwaway vault (local bare git repo) and the real test database."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from aegis.services import notes
from aegis.services.knowledge import _content_id_for
from aegis_worker.activities.notes import INDEX_STATE_KEY, NotesActivities
from temporalio.testing import ActivityEnvironment

from tests.notes_vault import CIPHER, device_commit, make_vault, needs_git, remote_file


class _KS:
    """Records what the index writes; stands in for KnowledgeStore."""

    def __init__(self):
        self.ingested: dict[str, dict] = {}
        self.deleted: list[str] = []

    async def ingest_content(self, **kwargs):
        self.ingested[kwargs["url"]] = kwargs
        return {"status": "ok", "content_id": _content_id_for(kwargs["url"])}

    async def delete_content(self, content_id):
        self.deleted.append(content_id)
        return True


@pytest.fixture
def vault(tmp_path):
    return make_vault(tmp_path)


@pytest_asyncio.fixture(loop_scope="function")
async def clean_state(db_pool):
    await db_pool.execute("DELETE FROM settings WHERE key = $1", INDEX_STATE_KEY)
    yield db_pool
    await db_pool.execute("DELETE FROM settings WHERE key = $1", INDEX_STATE_KEY)


# ------------------------------------------------------------ journal write


@needs_git
async def test_journal_write_appends_once_and_then_says_it_is_there(vault):
    acts = NotesActivities(settings=vault["settings"])
    entry = {"kind": "daily", "day": "2026-09-12", "label": "2026-09-12", "text": "A day."}
    first = await ActivityEnvironment().run(acts.notes_journal_write, entry)
    second = await ActivityEnvironment().run(acts.notes_journal_write, entry)
    assert first == {"status": "written", "path": "journal/12 Sep 26.md"}
    assert second == {"status": "exists", "path": "journal/12 Sep 26.md"}
    text = remote_file(vault, "journal/12 Sep 26.md")
    assert text.startswith("# Sep 12, 2026\n## Journal\n- the user wrote this\n")
    assert text.count("A day.") == 1


@needs_git
async def test_a_weekly_rollup_goes_to_the_vaults_weekly_note(vault):
    acts = NotesActivities(settings=vault["settings"])
    entry = {"kind": "weekly", "day": "2026-09-07", "label": "2026-W37", "text": "A week."}
    out = await ActivityEnvironment().run(acts.notes_journal_write, entry)
    assert out == {"status": "written", "path": "journal/2026/09. Sep/W37 Sep 26.md"}


async def test_journal_write_when_the_vault_is_off():
    out = await ActivityEnvironment().run(
        NotesActivities(settings=SimpleNamespace()).notes_journal_write,
        {"kind": "daily", "day": "2026-09-12", "label": "2026-09-12", "text": "x"},
    )
    assert out == {"status": "not_configured"}


@needs_git
async def test_journal_write_never_raises(vault):
    out = await ActivityEnvironment().run(
        NotesActivities(settings=vault["settings"]).notes_journal_write,
        {"kind": "yearly", "day": "2026-09-12", "label": "2026", "text": "x"},
    )
    assert out["status"] == "error"


async def test_a_chat_write_when_the_vault_is_off_on_the_worker():
    out = await ActivityEnvironment().run(
        NotesActivities(settings=SimpleNamespace()).notes_write,
        "write",
        {"path": "raphael/x.md", "text": "t", "heading": "h", "title": ""},
    )
    assert out["ok"] is False and "not configured on the worker" in out["message"]


# ------------------------------------------------------------------- index


@needs_git
async def test_the_index_works_through_a_pass_in_batches_and_never_stores_ciphertext(
    vault, clean_state
):
    ks = _KS()
    acts = NotesActivities(settings=vault["settings"], db_pool=clean_state, knowledge_connector=ks)
    first = await ActivityEnvironment().run(acts.notes_index_vault, 1)
    assert first["status"] == "ok" and first["full"] is True
    assert first["indexed"] == 1 and first["remaining"] == 1
    state = await clean_state.fetchval("SELECT value FROM settings WHERE key = $1", INDEX_STATE_KEY)
    assert state["done"] == 1 and len(state["todo"]) == 2

    second = await ActivityEnvironment().run(acts.notes_index_vault, 10)
    assert second["indexed"] == 1 and second["remaining"] == 0
    assert set(ks.ingested) == {"vault://journal/12 Sep 26.md", "vault://knowledge/dev/secrets.md"}
    secret = ks.ingested["vault://knowledge/dev/secrets.md"]
    for field in ("raw_text", "summary"):
        assert "c2VjcmV0" not in secret[field], f"ciphertext reached the store via {field}"
    assert secret["source_type"] == "note"
    assert secret["tags"] == ["note", "knowledge"]
    assert CIPHER not in str(ks.ingested)

    # Nothing changed: nothing is indexed again.
    third = await ActivityEnvironment().run(acts.notes_index_vault, 10)
    assert third["indexed"] == 0 and third["remaining"] == 0


@needs_git
async def test_a_deleted_note_leaves_the_index_and_a_changed_one_is_reindexed(
    vault, clean_state
):
    ks = _KS()
    acts = NotesActivities(settings=vault["settings"], db_pool=clean_state, knowledge_connector=ks)
    await ActivityEnvironment().run(acts.notes_index_vault, 10)
    ks.ingested.clear()
    device_commit(
        vault,
        {"knowledge/dev/secrets.md": None, "journal/12 Sep 26.md": "# Sep 12, 2026\nedited\n"},
    )
    out = await ActivityEnvironment().run(acts.notes_index_vault, 10)
    assert out["indexed"] == 1 and out["removed"] == 1
    assert list(ks.ingested) == ["vault://journal/12 Sep 26.md"]
    assert ks.deleted == [_content_id_for("vault://knowledge/dev/secrets.md")]


async def test_the_index_when_the_vault_is_off():
    out = await ActivityEnvironment().run(
        NotesActivities(settings=SimpleNamespace(), knowledge_connector=_KS()).notes_index_vault, 10
    )
    assert out == {"status": "not_configured"}


# ---------------------------------------------------------------- backfill


@pytest_asyncio.fixture(loop_scope="function")
async def daylog_rows(db_pool):
    async def wipe():
        await db_pool.execute(
            "DELETE FROM knowledge_content WHERE source_type IN ('daylog', 'daylog_rollup')"
        )

    await wipe()
    rows = [
        ("daylog", {"date": "2019-03-11"}, "Monday: shipped the migration."),
        (
            "daylog_rollup",
            {"period": "weekly", "label": "2019-W11", "start": "2019-03-11", "end": "2019-03-17"},
            "The week: one migration.",
        ),
    ]
    for source_type, meta, text in rows:
        cid = f"bf-{uuid4().hex[:8]}"
        await db_pool.execute(
            "INSERT INTO knowledge_content (content_id, url, title, source_type, metadata) "
            "VALUES ($1, $2, 'Day Log', $3, $4)",
            cid,
            f"aegis://daylog/{cid}",
            source_type,
            meta,
        )
        await db_pool.execute(
            "INSERT INTO knowledge_chunks (content_id, chunk_index, chunk_text) VALUES ($1, 0, $2)",
            cid,
            text,
        )
    yield db_pool
    await wipe()


@needs_git
async def test_backfill_writes_old_daylog_rows_into_the_journal_once(vault, daylog_rows):
    acts = NotesActivities(settings=vault["settings"], db_pool=daylog_rows)
    first = await ActivityEnvironment().run(acts.notes_backfill_journal, 100)
    assert first == {
        "status": "ok", "entries": 2, "written": 2, "already_there": 0, "skipped": 0,
    }
    day = remote_file(vault, notes.daily_note_path(notes.date(2019, 3, 11)))
    week = remote_file(vault, notes.weekly_note_path(notes.date(2019, 3, 11)))
    assert "Monday: shipped the migration." in day
    assert "The week: one migration." in week
    # The same marker the live daylog writes, so the live run is a no-op.
    assert notes.marker(notes.journal_key("daily", "2019-03-11")) in day

    again = await ActivityEnvironment().run(acts.notes_backfill_journal, 100)
    assert again["written"] == 0 and again["already_there"] == 2
