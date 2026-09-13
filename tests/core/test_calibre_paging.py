"""calibre-web's `next` link may not lead the connector — and the library's
credentials — to another host (#510 validation)."""

from __future__ import annotations

import httpx
import pytest
import respx
from aegis.connectors.calibre import CalibreConnector, CalibreError

from tests.core.test_calibre_connector import BASE, _entry


def _page(entries: str, next_href: str | None = None) -> str:
    nxt = (
        f'<link rel="next" title="Next" href="{next_href}" '
        'type="application/atom+xml;profile=opds-catalog;type=feed;kind=navigation"/>'
        if next_href
        else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:dcterms="http://purl.org/dc/terms/">'
        f"<id>urn:uuid:1</id><title>Calibre-Web</title>{nxt}{entries}</feed>"
    )


_ATOM = {"content-type": "application/atom+xml"}


@pytest.mark.asyncio
@respx.mock
async def test_a_next_link_to_another_host_is_not_followed_with_the_credentials():
    respx.get(url__startswith=f"{BASE}/opds/new").mock(
        return_value=httpx.Response(
            200, text=_page(_entry(1, 12, "Hands-On"), "http://evil.test/opds/new?offset=2"),
            headers=_ATOM,
        )
    )
    evil = respx.get(url__startswith="http://evil.test").mock(
        return_value=httpx.Response(200, text=_page(""), headers=_ATOM)
    )
    conn = CalibreConnector(BASE, "aegis", "secret")
    with pytest.raises(CalibreError, match="another host"):
        await conn.catalog()
    assert not evil.called, "the library's credentials went to another host"
    await conn.close()


@pytest.mark.asyncio
@respx.mock
async def test_an_absolute_next_link_on_calibre_web_itself_is_followed():
    def page(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("offset") == "2":
            return httpx.Response(200, text=_page(_entry(3, 3, "Fluent Python")), headers=_ATOM)
        return httpx.Response(
            200, text=_page(_entry(1, 12, "Hands-On"), f"{BASE}/opds/new?offset=2"), headers=_ATOM
        )

    respx.get(url__startswith=f"{BASE}/opds/new").mock(side_effect=page)
    conn = CalibreConnector(BASE, "aegis", "secret")
    assert [b["id"] for b in await conn.catalog()] == [12, 3]
    await conn.close()


@pytest.mark.asyncio
@respx.mock
async def test_a_download_link_to_another_host_is_refused_too():
    """The same rule as the `next` link: a book whose acquisition link names
    another host is not fetched with the library's credentials."""
    book = {
        "id": 1,
        "title": "T",
        "formats": [{"format": "EPUB", "href": "http://evil.test/opds/download/1/epub/"}],
    }
    evil = respx.get(url__startswith="http://evil.test").mock(return_value=httpx.Response(200))
    conn = CalibreConnector(BASE, "aegis", "secret")
    with pytest.raises(CalibreError, match="another host"):
        await conn.download(book, "epub")
    assert not evil.called
    await conn.close()


def test_the_page_cap_follows_the_book_cap():
    """`calibre_max_books` is the Integrations key; the connector turns it
    into pages at calibre-web's 60 a page (3,000 → 50, the old `_MAX_PAGES`)."""
    assert CalibreConnector(BASE, "u", "p").max_pages == 50
    assert CalibreConnector(BASE, "u", "p", max_books=61).max_pages == 2
    assert CalibreConnector(BASE, "u", "p", max_books=0).max_pages == 50
