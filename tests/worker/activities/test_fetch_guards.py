"""Every fetch of a URL a third party publishes goes through `url_guard`, the
first request and every redirect: the feed poll (a feed URL is the feed's
own) and the media download behind the ElevenLabs transcription. Both used to
fetch with a client that followed a public URL's redirect wherever it led."""

from __future__ import annotations

import httpx
import pytest
import respx
from aegis.services import url_guard
from aegis_worker.activities import rss as rss_mod
from aegis_worker.activities.content import _ELEVENLABS_STT_URL, _transcribe_via_elevenlabs
from aegis_worker.activities.rss import FetchFeedInput, RssActivities
from temporalio import workflow
from temporalio.testing import ActivityEnvironment

with workflow.unsafe.imports_passed_through():
    from tests.worker.flows.test_rss_feeds_flow import Rec, _run, _stubs

pytestmark = pytest.mark.asyncio

FEED = "https://feed.example.com/rss"
PUBLIC = "93.184.215.14"
RSS = (
    '<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>'
    "<item><title>One</title><link>https://feed.example.com/1</link><guid>g1</guid>"
    "<pubDate>Thu, 10 Sep 2026 10:00:00 GMT</pubDate></item>"
    "</channel></rss>"
)


@pytest.fixture(autouse=True)
def dns(monkeypatch):
    """Every name resolves to a public address; an IP literal is checked as is."""

    async def fake_resolve(host: str, port: int) -> list[str]:
        return [PUBLIC]

    monkeypatch.setattr(url_guard, "resolve_host", fake_resolve)


async def _fetch(url: str = FEED):
    return await ActivityEnvironment().run(
        RssActivities(db_pool=None).fetch_feed, FetchFeedInput(url=url)
    )


@respx.mock
async def test_a_feed_is_fetched_over_the_guarded_client_and_parsed():
    respx.get(FEED).mock(return_value=httpx.Response(200, text=RSS))
    result = await _fetch()
    assert result.error == ""
    assert [e["title"] for e in result.entries] == ["One"]
    assert result.entries[0]["published"].startswith("2026-09-10T10:00")


@respx.mock
async def test_a_redirect_to_a_private_address_is_refused_and_is_a_failed_fetch():
    respx.get(FEED).mock(
        return_value=httpx.Response(302, headers={"Location": "http://10.0.0.5/admin.xml"})
    )
    inner = respx.get("http://10.0.0.5/admin.xml").mock(return_value=httpx.Response(200, text=RSS))
    result = await _fetch()
    assert not inner.called, "the inward hop must never be made"
    assert result.entries == []
    assert "refused" in result.error and "non-public" in result.error


@respx.mock
async def test_a_feed_url_on_the_overlay_network_is_refused_before_any_request(monkeypatch):
    inner = respx.get("http://calibre-web_calibre-web:8083/opds").mock(
        return_value=httpx.Response(200, text=RSS)
    )

    async def private(host: str, port: int) -> list[str]:
        return ["10.0.1.7"]

    monkeypatch.setattr(url_guard, "resolve_host", private)
    result = await _fetch("http://calibre-web_calibre-web:8083/opds")
    assert not inner.called
    assert result.entries == [] and "refused" in result.error


@respx.mock
async def test_an_http_error_is_a_failed_fetch_even_with_a_feed_body():
    respx.get(FEED).mock(return_value=httpx.Response(404, text=RSS))
    result = await _fetch()
    assert (result.entries, result.error) == ([], "HTTP 404")


@respx.mock
async def test_a_response_over_the_size_cap_is_a_failed_fetch(monkeypatch):
    monkeypatch.setattr(rss_mod, "_FEED_MAX_BYTES", 100)
    respx.get(FEED).mock(return_value=httpx.Response(200, text=RSS))
    result = await _fetch()
    assert result.entries == []
    assert "larger than" in result.error


@respx.mock
async def test_a_refused_redirect_is_recorded_as_a_failed_fetch_by_the_flow():
    """The real `fetch_feed` inside RssIngestFlow: the refusal is a failed
    fetch on the feed's record, as a dead host is."""
    respx.get(FEED).mock(
        return_value=httpx.Response(302, headers={"Location": "http://169.254.169.254/latest"})
    )
    rec = Rec()
    stubs = [s for s in _stubs(rec, config={"label": "Bounced"}) if s.__name__ != "fetch"]
    stubs.append(RssActivities(db_pool=None).fetch_feed)
    import tests.worker.flows.test_rss_feeds_flow as harness

    old_feed = harness.FEED
    harness.FEED = FEED
    try:
        result = await _run(stubs, "rf-guard-refused")
    finally:
        harness.FEED = old_feed
    assert result["errors"] == 1
    assert len(rec.runs) == 1 and rec.runs[0]["ok"] is False
    assert "refused" in rec.runs[0]["error"]


@respx.mock
async def test_media_redirected_to_a_private_address_is_never_downloaded_or_transcribed():
    respx.get("https://media.example.com/talk.mp3").mock(
        return_value=httpx.Response(302, headers={"Location": "http://169.254.169.254/latest"})
    )
    inner = respx.get("http://169.254.169.254/latest").mock(
        return_value=httpx.Response(200, content=b"secret")
    )
    stt = respx.post(_ELEVENLABS_STT_URL).mock(
        return_value=httpx.Response(200, json={"text": "a transcript"})
    )
    out = await _transcribe_via_elevenlabs("https://media.example.com/talk.mp3", "k")
    assert out is None
    assert not inner.called, "the inward hop must never be made"
    assert not stt.called
