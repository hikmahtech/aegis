"""Tests for the content-extraction helpers."""

from __future__ import annotations

import httpx
import respx
from aegis.services.content_extract import extract_bytes, extract_html, fetch_and_extract

_ARTICLE = """
<html><head><title>The Title</title></head><body>
<article>
<h1>About Aardvarks</h1>
<p>Aardvarks are nocturnal mammals native to Africa. They dig burrows with
powerful claws and feed almost entirely on ants and termites at night.</p>
<p>An adult aardvark can consume tens of thousands of insects in a single
evening, using a long sticky tongue to extract them from their mounds.</p>
</article></body></html>
"""


def test_extract_bytes_plaintext():
    text, title = extract_bytes(b"# Heading\nhello world", filename="notes.md")
    assert "hello world" in text
    assert title is None


def test_extract_html_pulls_body_and_title():
    text, title = extract_html(_ARTICLE)
    assert "aardvark" in text.lower()
    assert "nocturnal" in text.lower()
    assert title and "aardvark" in title.lower()  # trafilatura prefers the h1


async def test_fetch_and_extract_plaintext():
    with respx.mock:
        respx.get("http://x/doc.txt").mock(
            return_value=httpx.Response(200, headers={"content-type": "text/plain"}, text="just words")
        )
        text, _ = await fetch_and_extract("http://x/doc.txt")
    assert text == "just words"


async def test_fetch_and_extract_image_returns_empty():
    with respx.mock:
        respx.get("http://x/p.png").mock(
            return_value=httpx.Response(200, headers={"content-type": "image/png"}, content=b"\x89PNG")
        )
        text, title = await fetch_and_extract("http://x/p.png")
    assert text == "" and title is None


async def test_fetch_and_extract_handles_fetch_failure():
    with respx.mock:
        respx.get("http://x/dead").mock(return_value=httpx.Response(500))
        text, title = await fetch_and_extract("http://x/dead")
    assert text == "" and title is None


async def test_fetch_and_extract_pdf_magic_byte_sniff(monkeypatch):
    """PDFs served without a pdf content-type are caught by the %PDF- sniff."""
    from aegis.services import content_extract

    monkeypatch.setattr(content_extract, "extract_pdf", lambda data, max_chars=100_000: "PDF TEXT")
    with respx.mock:
        respx.get("http://x/f").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "application/octet-stream"},
                content=b"%PDF-1.4 fake",
            )
        )
        text, title = await content_extract.fetch_and_extract("http://x/f")
    assert text == "PDF TEXT" and title is None


# --- YouTube helpers ---------------------------------------------------------


def test_extract_youtube_id_variants():
    from aegis.services.content_extract import extract_youtube_id

    vid = "dQw4w9WgXcQ"
    for url in (
        f"https://www.youtube.com/watch?v={vid}",
        f"https://youtu.be/{vid}",
        f"https://www.youtube.com/shorts/{vid}",
        f"https://www.youtube.com/embed/{vid}",
        f"https://www.youtube.com/live/{vid}",
    ):
        assert extract_youtube_id(url) == vid, url
    assert extract_youtube_id("https://example.com/watch?v=short") is None
    assert extract_youtube_id("") is None


def _fake_yt_module(snippets=None, exc=None):
    """A stand-in youtube_transcript_api module (avoids real network/library)."""
    import types

    class _Api:
        def fetch(self, video_id):
            if exc:
                raise exc
            return snippets

    mod = types.ModuleType("youtube_transcript_api")
    mod.YouTubeTranscriptApi = _Api
    return mod


async def test_fetch_youtube_transcript_success(monkeypatch):
    import sys
    from types import SimpleNamespace

    from aegis.services import content_extract

    snippets = [SimpleNamespace(text="hello"), SimpleNamespace(text="world")]
    monkeypatch.setitem(sys.modules, "youtube_transcript_api", _fake_yt_module(snippets))
    text, meta = await content_extract.fetch_youtube_transcript("https://youtu.be/dQw4w9WgXcQ")
    assert text == "hello world"
    assert meta == {"video_id": "dQw4w9WgXcQ", "segments": 2}


async def test_fetch_youtube_transcript_no_captions(monkeypatch):
    import sys

    from aegis.services import content_extract

    monkeypatch.setitem(
        sys.modules, "youtube_transcript_api", _fake_yt_module(exc=RuntimeError("no captions"))
    )
    text, meta = await content_extract.fetch_youtube_transcript("https://youtu.be/dQw4w9WgXcQ")
    assert text == ""
    assert meta["video_id"] == "dQw4w9WgXcQ"


async def test_fetch_youtube_transcript_non_youtube_url():
    from aegis.services.content_extract import fetch_youtube_transcript

    text, meta = await fetch_youtube_transcript("https://example.com/video")
    assert text == "" and meta == {}
