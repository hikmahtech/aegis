"""Tests for the Drive-watch incrementals: recursion + skip-unchanged."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aegis.services import drive
from aegis.services.knowledge import _content_id_for

pytestmark = pytest.mark.asyncio

_DOC = "application/vnd.google-apps.document"
_FOLDER = "application/vnd.google-apps.folder"


class _Files:
    def __init__(self, by_parent, contents):
        self.by_parent, self.contents = by_parent, contents

    def list(self, q, fields, pageSize, pageToken):  # noqa: N803 — Drive API kwargs
        parent = re.search(r"'([^']+)' in parents", q).group(1)
        m = MagicMock()
        m.execute.return_value = {"files": self.by_parent.get(parent, []), "nextPageToken": None}
        return m

    def export(self, fileId, mimeType):  # noqa: N803
        m = MagicMock()
        m.execute.return_value = self.contents[fileId]
        return m

    def get_media(self, fileId):  # noqa: N803
        m = MagicMock()
        m.execute.return_value = self.contents[fileId]
        return m


class _Drive:
    def __init__(self, by_parent, contents):
        self._f = _Files(by_parent, contents)

    def files(self):
        return self._f


class _Store:
    def __init__(self):
        self.calls = []

    async def ingest_content(self, **kw):
        self.calls.append(kw)
        return {"content_id": "x", "status": "ok", "chunks_total": 1}


def _patch(monkeypatch, by_parent, contents):
    monkeypatch.setattr(drive, "_build_drive_service", lambda tp: _Drive(by_parent, contents))


async def test_recursion_descends_into_subfolders(monkeypatch):
    by_parent = {
        "root": [
            {"id": "doc1", "name": "Top", "mimeType": _DOC, "modifiedTime": "T1"},
            {"id": "sub", "name": "Sub", "mimeType": _FOLDER, "modifiedTime": "T0"},
        ],
        "sub": [{"id": "txt1", "name": "deep.txt", "mimeType": "text/plain", "modifiedTime": "T1"}],
    }
    _patch(monkeypatch, by_parent, {"doc1": b"top doc body", "txt1": b"deep file body"})
    store = _Store()

    flat = await drive.ingest_drive_folder(store, Path("/t"), "root", recurse=False)
    assert flat["ingested"] == 1  # subfolder excluded, not descended

    store2 = _Store()
    monkeypatch.setattr(drive, "_build_drive_service", lambda tp: _Drive(by_parent, {"doc1": b"top doc body", "txt1": b"deep file body"}))
    deep = await drive.ingest_drive_folder(store2, Path("/t"), "root", recurse=True)
    assert deep["ingested"] == 2  # top doc + the file inside the subfolder


async def test_skip_unchanged_by_modified_time(monkeypatch):
    by_parent = {"root": [{"id": "doc1", "name": "D", "mimeType": _DOC, "modifiedTime": "T1"}]}
    _patch(monkeypatch, by_parent, {"doc1": b"some body text"})
    seen = {_content_id_for("gdrive://doc1"): "T1"}

    store = _Store()
    res = await drive.ingest_drive_folder(store, Path("/t"), "root", skip_unchanged=seen)
    assert res["unchanged"] == 1 and res["ingested"] == 0 and store.calls == []

    # a different modifiedTime → re-ingested
    store2 = _Store()
    res2 = await drive.ingest_drive_folder(
        store2, Path("/t"), "root", skip_unchanged={_content_id_for("gdrive://doc1"): "OLD"}
    )
    assert res2["ingested"] == 1
    assert store2.calls[0]["metadata"]["drive_modified_time"] == "T1"
