"""CalibreActivities.sync_calibre_library — the book index follows the library (#510).

The index rows live in a real Postgres (the activity reads them with SQL); the
knowledge store's writes and calibre-web itself are fakes.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aegis.services import library
from aegis.services.knowledge import _content_id_for
from aegis_worker.activities import calibre as calibre_mod
from aegis_worker.activities.calibre import CalibreActivities
from temporalio.testing import ActivityEnvironment


def _book(book_id: int, title: str, description: str = "About it.") -> dict:
    return {
        "id": book_id,
        "uuid": f"u-{book_id}",
        "title": title,
        "authors": ["A. Author"],
        "tags": ["ml"],
        "published": "2020-01-01",
        "updated": "2026-01-01T00:00:00+00:00",
        "description": description,
        "formats": [{"format": "PDF", "href": f"x/{book_id}", "size": 1}],
    }


@pytest_asyncio.fixture(loop_scope="function")
async def book_rows(db_pool):
    """Index rows for books 1-4; book 2 is indexed from an older description."""
    await db_pool.execute("DELETE FROM knowledge_content WHERE source_type = 'book'")
    for book in (_book(1, "One"), _book(2, "Two", "Old words."), _book(3, "Three"), _book(4, "Four")):
        doc = library.book_document(book)
        await db_pool.execute(
            "INSERT INTO knowledge_content (content_id, url, title, source_type, summary, tags, metadata) "
            "VALUES ($1, $2, $3, 'book', $4, $5, $6)",
            _content_id_for(doc["url"]),
            doc["url"],
            doc["title"],
            doc["summary"],
            doc["tags"],
            doc["metadata"],
        )
    try:
        yield db_pool
    finally:
        await db_pool.execute("DELETE FROM knowledge_content WHERE source_type = 'book'")


@pytest.fixture
def health(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(calibre_mod, "record_connector_health", mock)
    return mock


def _with_catalog(monkeypatch, books=None, raises=None):
    conn = AsyncMock()
    conn.catalog = AsyncMock(side_effect=raises) if raises else AsyncMock(return_value=books)
    monkeypatch.setattr(library, "connector_or_reason", lambda settings: (conn, ""))
    return conn


def _kc():
    kc = AsyncMock()
    kc.ingest_content = AsyncMock(return_value={"status": "ok"})
    kc.delete_content = AsyncMock(return_value=True)
    return kc


@pytest.mark.asyncio
async def test_not_configured_is_a_reported_no_op(monkeypatch):
    monkeypatch.setattr(library, "connector_or_reason", lambda settings: (None, "not_configured"))
    act = CalibreActivities(knowledge_connector=_kc(), db_pool=object(), settings=SimpleNamespace())
    assert await ActivityEnvironment().run(act.sync_calibre_library) == {"status": "not_configured"}


@pytest.mark.asyncio
async def test_the_index_follows_the_library(book_rows, monkeypatch, health):
    # Library: 1 unchanged, 2 changed, 3 unchanged, 5 new; 4 has left Calibre.
    _with_catalog(
        monkeypatch,
        [_book(1, "One"), _book(2, "Two", "New words."), _book(3, "Three"), _book(5, "Five")],
    )
    kc = _kc()
    act = CalibreActivities(knowledge_connector=kc, db_pool=book_rows, settings=SimpleNamespace())
    out = await ActivityEnvironment().run(act.sync_calibre_library)

    assert out == {
        "status": "ok",
        "books": 4,
        "added": 1,
        "updated": 1,
        "unchanged": 2,
        "failed": 0,
        "removed": 1,
        "removed_titles": ["Four"],
    }
    written = sorted(c.kwargs["url"] for c in kc.ingest_content.await_args_list)
    assert written == ["calibre://book/2", "calibre://book/5"]
    assert kc.ingest_content.await_args.kwargs["source_type"] == "book"
    kc.delete_content.assert_awaited_once_with(_content_id_for("calibre://book/4"))
    assert health.await_args.kwargs["ok"] is True


@pytest.mark.asyncio
async def test_a_short_catalogue_is_not_trusted_to_delete(book_rows, monkeypatch, health):
    _with_catalog(monkeypatch, [_book(1, "One")])  # 1 book against 4 indexed
    kc = _kc()
    act = CalibreActivities(knowledge_connector=kc, db_pool=book_rows, settings=SimpleNamespace())
    out = await ActivityEnvironment().run(act.sync_calibre_library)
    assert out["removal_withheld"] == 3
    assert "removed" not in out
    kc.delete_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_bad_book_does_not_cost_the_rest(book_rows, monkeypatch, health):
    _with_catalog(monkeypatch, [_book(1, "One"), _book(6, "Six"), _book(7, "Seven")])
    kc = _kc()
    kc.ingest_content = AsyncMock(side_effect=[RuntimeError("embed failed"), {"status": "ok"}])
    act = CalibreActivities(knowledge_connector=kc, db_pool=book_rows, settings=SimpleNamespace())
    out = await ActivityEnvironment().run(act.sync_calibre_library)
    assert out["failed"] == 1 and out["added"] == 1 and out["unchanged"] == 1


@pytest.mark.asyncio
async def test_an_unreadable_library_fails_the_run_and_records_health(book_rows, monkeypatch, health):
    _with_catalog(monkeypatch, raises=RuntimeError("calibre-web is unreachable"))
    act = CalibreActivities(knowledge_connector=_kc(), db_pool=book_rows, settings=SimpleNamespace())
    with pytest.raises(RuntimeError, match="unreachable"):
        await ActivityEnvironment().run(act.sync_calibre_library)
    assert health.await_args.kwargs["ok"] is False
