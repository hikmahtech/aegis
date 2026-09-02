"""fetch_meeting_document — Gmail body → Docs link → Drive export, with every
failure mapped to a doc_status and a body-only fallback."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest
from aegis_worker.activities import meeting as m
from aegis_worker.activities.meeting import MeetingActivities

pytestmark = pytest.mark.asyncio

DOC_ID = "1AbC_d-9xyz"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
DOC_TEXT = (
    "Notes\n* one\n* two\n\n"
    "A Person: hello there\nB Person: hi\nA Person: how are things\nB Person: fine\n"
)
MSG = {
    "id": "gm-1",
    "subject": "Notes: “Widget Standup” Sep 1, 2026",
    "snippet": "These notes have been sent",
    "internal_date_ms": 1_788_000_000_000,
}


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def _payload(plain: str, html: str | None) -> dict:
    parts = [{"mimeType": "text/plain", "body": {"data": _b64(plain)}}]
    if html is not None:
        parts.append({"mimeType": "text/html", "body": {"data": _b64(html)}})
    return {"mimeType": "multipart/alternative", "parts": parts}


class _FakeGmail:
    def __init__(self, payload: dict):
        self._payload = payload

    def users(self):
        return self

    def messages(self):
        return self

    def get(self, **_kw):
        return self

    def execute(self):
        return {"payload": self._payload}


class _Http(Exception):  # noqa: N818 — stands in for googleapiclient's HttpError
    def __init__(self, status: int, content: bytes = b""):
        self.resp = SimpleNamespace(status=status)
        self.content = content


@pytest.fixture
def token(tmp_path):
    p = tmp_path / "acct.json"
    p.write_text(json.dumps({"scopes": [DRIVE_SCOPE]}))
    return p


def _act(tmp_path) -> MeetingActivities:
    return MeetingActivities(gmail_credentials_file="creds.json", gmail_token_dir=str(tmp_path))


def _wire(monkeypatch, payload, export):
    monkeypatch.setattr(m, "_build_gmail_service", lambda *_a: _FakeGmail(payload))
    monkeypatch.setattr(m, "_export_doc", export)


async def test_ok_path_exports_the_doc_and_splits_transcript(monkeypatch, tmp_path, token):
    html = f'<a href="https://docs.google.com/document/d/{DOC_ID}/edit">Open meeting notes</a>'
    seen = {}

    def export(token_path, doc_id):
        seen["args"] = (token_path, doc_id)
        return "Widget Standup – Notes by Gemini", "2026-09-01T09:06:33Z", DOC_TEXT

    _wire(monkeypatch, _payload("Open meeting notes", html), export)
    out = await _act(tmp_path).fetch_meeting_document("acct", MSG)
    assert seen["args"] == (token, DOC_ID)
    assert out["doc_status"] == "ok"
    assert out["doc_id"] == DOC_ID
    assert out["doc_url"].endswith(DOC_ID)
    assert out["title"] == "Widget Standup – Notes by Gemini"
    assert out["doc_modified_time"] == "2026-09-01T09:06:33Z"
    assert out["notes"] == "Notes\n* one\n* two"
    assert out["transcript"][0] == ["A Person", "hello there"] or out["transcript"][0] == ("A Person", "hello there")
    assert out["speakers"] == ["A Person", "B Person"]
    assert out["meeting_date"].startswith("2026-")


async def test_a_doc_with_no_preamble_gets_a_header_not_the_transcript(monkeypatch, tmp_path, token):
    """A doc that opens straight on a speaker line has empty split notes. `notes`
    is filed as knowledge_content, and the transcript must never reach it."""
    only_transcript = (
        "A Person: hello there\nB Person: hi\nA Person: how are things\nB Person: fine\n"
    )
    _wire(
        monkeypatch,
        _payload("body", f"https://docs.google.com/document/d/{DOC_ID}"),
        lambda *_a: ("Standup", "2026-09-01T09:06:33Z", only_transcript),
    )
    out = await _act(tmp_path).fetch_meeting_document("acct", MSG)
    assert out["doc_status"] == "ok"
    assert out["notes"] == "Speakers: A Person, B Person"
    assert "hello there" not in out["notes"]
    assert len(out["transcript"]) == 4


async def test_no_link_falls_back_to_body(monkeypatch, tmp_path, token):
    _wire(monkeypatch, _payload("Plain summary body only", None), lambda *_a: pytest.fail("must not export"))
    out = await _act(tmp_path).fetch_meeting_document("acct", MSG)
    assert out["doc_status"] == "no_link"
    assert out["doc_id"] == ""
    assert out["notes"] == "Plain summary body only"
    assert out["transcript"] == [] and out["speakers"] == []
    assert out["title"] == MSG["subject"]


async def test_token_without_drive_scope_is_reported_before_any_export(monkeypatch, tmp_path):
    (tmp_path / "acct.json").write_text(json.dumps({"scopes": ["https://www.googleapis.com/auth/gmail.modify"]}))
    html = f"https://docs.google.com/document/d/{DOC_ID}"
    _wire(monkeypatch, _payload("body", html), lambda *_a: pytest.fail("must not export"))
    out = await _act(tmp_path).fetch_meeting_document("acct", MSG)
    assert out["doc_status"] == "no_drive_scope"
    assert out["doc_id"] == DOC_ID
    assert out["notes"] == "body"


@pytest.mark.parametrize(
    "exc,status",
    [
        (_Http(404), "inaccessible"),
        (_Http(403, b'{"error": {"message": "The caller does not have permission"}}'), "inaccessible"),
        (_Http(403, b"Request had insufficient authentication scopes."), "no_drive_scope"),
        (_Http(500), "fetch_failed"),
        (RuntimeError("boom"), "fetch_failed"),
    ],
)
async def test_export_errors_map_to_doc_status_and_keep_the_body(monkeypatch, tmp_path, token, exc, status):
    def export(*_a):
        raise exc

    _wire(monkeypatch, _payload("body text", f"https://docs.google.com/document/d/{DOC_ID}"), export)
    out = await _act(tmp_path).fetch_meeting_document("acct", MSG)
    assert out["doc_status"] == status
    assert out["notes"] == "body text"


async def test_gmail_read_failure_is_fetch_failed_with_snippet(monkeypatch, tmp_path, token):
    def boom(*_a):
        raise RuntimeError("token_missing")

    monkeypatch.setattr(m, "_build_gmail_service", boom)
    out = await _act(tmp_path).fetch_meeting_document("acct", MSG)
    assert out["doc_status"] == "fetch_failed"
    assert out["notes"] == MSG["snippet"]
