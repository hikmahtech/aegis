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
