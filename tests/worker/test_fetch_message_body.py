"""fetch_message_body: the full email text for the money extractor (spec §2 step 2)."""

from __future__ import annotations

import base64

import pytest
from aegis_worker.activities.gmail import GmailActivities, html_to_text
from temporalio.testing import ActivityEnvironment

HTML = (
    "<html><head><style>.x{color:red}</style></head><body>"
    "<p>Dear Customer,</p><p>Rs.10.00 is debited from your account ending 1225 "
    "towards VPA q203028199@ybl (Jai shree nakoda) on 02-09-26.</p>"
    "<a href='https://example.com/track?id=1'>https://example.com/track?id=1</a>"
    "<script>alert(1)</script>&nbsp;&nbsp;Regards,&amp; HDFC</body></html>"
)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


class _FakeSvc:
    def __init__(self, payload: dict, raise_exc: Exception | None = None):
        self._payload = payload
        self._raise = raise_exc

    def users(self):
        return self

    def messages(self):
        return self

    def get(self, **_kw):
        return self

    def execute(self):
        if self._raise:
            raise self._raise
        return {"payload": self._payload, "snippet": "snip"}


def _acts(monkeypatch, tmp_path, svc):
    (tmp_path / "acct.json").write_text("{}")
    monkeypatch.setattr("aegis_worker.activities.gmail._build_gmail_service", lambda *_a: svc)
    return GmailActivities(gmail_credentials_file="c.json", gmail_token_dir=str(tmp_path))


def test_html_to_text_strips_markup_scripts_and_urls():
    text = html_to_text(HTML)
    assert "Dear Customer," in text
    assert "Rs.10.00 is debited" in text
    assert "color:red" not in text
    assert "alert(1)" not in text
    assert "example.com" not in text and "<url>" in text
    assert "Regards,& HDFC" in text
    assert "  " not in text


@pytest.mark.asyncio
async def test_plain_part_wins_over_html(monkeypatch, tmp_path):
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64("plain body\n\n\nend")}},
            {"mimeType": "text/html", "body": {"data": _b64(HTML)}},
        ],
    }
    acts = _acts(monkeypatch, tmp_path, _FakeSvc(payload))
    body = await ActivityEnvironment().run(acts.fetch_message_body, "acct", "m1")
    assert body == "plain body\nend"


@pytest.mark.asyncio
async def test_html_only_message_is_reduced_to_text(monkeypatch, tmp_path):
    payload = {"mimeType": "text/html", "body": {"data": _b64(HTML)}}
    acts = _acts(monkeypatch, tmp_path, _FakeSvc(payload))
    body = await ActivityEnvironment().run(acts.fetch_message_body, "acct", "m1")
    assert body.startswith("Dear Customer,")
    assert "<p>" not in body


@pytest.mark.asyncio
async def test_max_chars_truncates(monkeypatch, tmp_path):
    payload = {"mimeType": "text/plain", "body": {"data": _b64("x" * 10_000)}}
    acts = _acts(monkeypatch, tmp_path, _FakeSvc(payload))
    body = await ActivityEnvironment().run(acts.fetch_message_body, "acct", "m1", 100)
    assert len(body) == 100


@pytest.mark.asyncio
async def test_failure_returns_empty_string(monkeypatch, tmp_path):
    acts = _acts(monkeypatch, tmp_path, _FakeSvc({}, raise_exc=RuntimeError("gmail down")))
    body = await ActivityEnvironment().run(acts.fetch_message_body, "acct", "m1")
    assert body == ""
