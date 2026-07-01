"""Tests for Google Drive folder ingestion (mocked Drive API)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aegis.services import drive

pytestmark = pytest.mark.asyncio

_DOC_MIME = "application/vnd.google-apps.document"


class _FakeFiles:
    def __init__(self, listing, contents):
        self._listing, self._contents = listing, contents

    def list(self, **kw):
        m = MagicMock()
        m.execute.return_value = {"files": self._listing, "nextPageToken": None}
        return m

    def export(self, fileId, mimeType):  # noqa: N803 — matches the Drive API kwargs
        m = MagicMock()
        m.execute.return_value = self._contents[fileId]
        return m

    def get_media(self, fileId):  # noqa: N803 — matches the Drive API kwargs
        m = MagicMock()
        m.execute.return_value = self._contents[fileId]
        return m


class _FakeDrive:
    def __init__(self, listing, contents):
        self._f = _FakeFiles(listing, contents)

    def files(self):
        return self._f


class _FakeStore:
    def __init__(self):
        self.calls = []

    async def ingest_content(self, **kw):
        self.calls.append(kw)
        return {"content_id": "x", "status": "ok", "chunks_total": 1}


async def test_ingest_drive_folder_exports_docs_downloads_files_skips_unsupported(monkeypatch):
    listing = [
        {"id": "doc1", "name": "Notes", "mimeType": _DOC_MIME},
        {"id": "txt1", "name": "a.txt", "mimeType": "text/plain"},
        {"id": "img1", "name": "pic.png", "mimeType": "image/png"},  # unsupported ext
    ]
    contents = {"doc1": b"exported google doc body", "txt1": b"plain file body"}
    monkeypatch.setattr(drive, "_build_drive_service", lambda tp: _FakeDrive(listing, contents))

    store = _FakeStore()
    res = await drive.ingest_drive_folder(store, Path("/tok.json"), "folder123")

    assert res == {"ingested": 2, "skipped": 1, "unchanged": 0, "errors": 0}
    urls = {c["url"] for c in store.calls}
    assert urls == {"gdrive://doc1", "gdrive://txt1"}
    doc = next(c for c in store.calls if c["url"] == "gdrive://doc1")
    assert "exported google doc body" in doc["raw_text"]
    assert doc["metadata"]["via"] == "drive"


async def test_ingest_drive_folder_counts_extract_failures_as_skips(monkeypatch):
    # An empty-extract file (image bytes via a .json name → decodes but empty after strip)
    listing = [{"id": "e1", "name": "empty.txt", "mimeType": "text/plain"}]
    monkeypatch.setattr(drive, "_build_drive_service", lambda tp: _FakeDrive(listing, {"e1": b"   "}))
    store = _FakeStore()
    res = await drive.ingest_drive_folder(store, Path("/tok.json"), "f")
    assert res["ingested"] == 0 and res["skipped"] == 1
    assert store.calls == []
