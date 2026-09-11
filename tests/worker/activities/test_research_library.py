"""ResearchFlow's gather step reads the Calibre library too (#510)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aegis.services import library
from aegis.services import research as rs
from aegis_worker.activities.research import ResearchActivities
from temporalio.testing import ActivityEnvironment


def _kc(similarity: float):
    async def search(query, limit=5, source_type=None, **_):
        if source_type == "book":
            return [
                {
                    "title": "Hands-On ML",
                    "summary": "A practical book.",
                    "similarity": similarity,
                    "metadata": {"calibre_id": 12, "authors": ["A"], "tags": ["ml"]},
                }
            ]
        return []

    kc = AsyncMock()
    kc.search = AsyncMock(side_effect=search)
    return kc


@pytest.fixture(autouse=True)
def no_papers(monkeypatch):
    monkeypatch.setattr(rs, "paper_search", AsyncMock(return_value={"papers": [], "errors": []}))


def _act(kc) -> ResearchActivities:
    return ResearchActivities(knowledge_connector=kc, settings=SimpleNamespace())


@pytest.mark.asyncio
async def test_a_close_book_gets_a_passage(monkeypatch):
    monkeypatch.setattr(library, "connector_or_reason", lambda settings: (object(), ""))
    read = AsyncMock(
        return_value={
            "passages": [
                {"cite": "Hands-On ML, chapter 4 (Training)", "text": "Tune the learning rate first."}
            ]
        }
    )
    monkeypatch.setattr(library, "read_book", read)
    out = await ActivityEnvironment().run(
        _act(_kc(0.8)).research_gather, {"question": "how should I tune the learning rate"}
    )
    book = out["books"][0]
    assert book["url"] == "calibre://book/12"
    assert book["cite"] == "Hands-On ML, chapter 4 (Training)"
    assert "Tune the learning rate first." in book["passage"]
    assert read.await_args.kwargs["pdf_scan_pages"] == library.RESEARCH_PDF_SCAN_PAGES


@pytest.mark.asyncio
async def test_a_distant_book_is_listed_but_not_read(monkeypatch):
    read = AsyncMock()
    monkeypatch.setattr(library, "read_book", read)
    monkeypatch.setattr(library, "connector_or_reason", lambda settings: (object(), ""))
    out = await ActivityEnvironment().run(
        _act(_kc(0.2)).research_gather, {"question": "how should I tune the learning rate"}
    )
    assert out["books"][0]["id"] == 12 and "passage" not in out["books"][0]
    read.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_library_that_fails_is_an_error_line(monkeypatch):
    monkeypatch.setattr(library, "connector_or_reason", lambda settings: (object(), ""))
    monkeypatch.setattr(library, "read_book", AsyncMock(side_effect=RuntimeError("calibre down")))
    out = await ActivityEnvironment().run(
        _act(_kc(0.9)).research_gather, {"question": "how should I tune the learning rate"}
    )
    assert out["books"][0]["id"] == 12
    assert any(e.startswith("library: calibre down") for e in out["errors"])
