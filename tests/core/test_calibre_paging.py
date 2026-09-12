"""calibre-web's `next` link may not lead the connector — and the library's
credentials — to another host (#510 validation)."""

from __future__ import annotations

import httpx
import pytest
import respx
from aegis.connectors.calibre import CalibreConnector, CalibreError, refuse_public_host

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


def test_the_public_host_is_refused_with_a_trailing_dot_or_capitals():
    for url in ("https://calibre.hikmahtech.in./opds", "https://CALIBRE.hikmahtech.in/opds"):
        with pytest.raises(ValueError):
            refuse_public_host(url)
