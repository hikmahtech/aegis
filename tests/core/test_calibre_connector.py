"""CalibreConnector — the read-only OPDS client for calibre-web (#510).

The feed fixtures follow calibre-web's own `templates/feed.xml`: Atom entries
with `urn:uuid:` ids, one `<category>` per tag, the description as an escaped
HTML paragraph inside an XHTML `<content>` div, and one acquisition link per
format at `/opds/download/<id>/<format>/`.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from aegis.connectors.calibre import (
    CalibreConnector,
    CalibreError,
    parse_feed,
)

BASE = "http://calibre.test"

_ENTRY = """
  <entry>
    <title>{title}</title>
    <id>urn:uuid:{uuid}</id>
    <updated>2026-01-0{n}T10:00:00+00:00</updated>
    <author><name>{author}</name></author>
    <publisher><name>O'Reilly</name></publisher>
    <published>2019-05-01T00:00:00+00:00</published>
    <dcterms:language>eng</dcterms:language>
    <category scheme="http://www.bisg.org/standards/bisac_subject/index.html" term="machine-learning" label="machine-learning"/>
    <category scheme="http://www.bisg.org/standards/bisac_subject/index.html" term="python" label="python"/>
    <content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">
      TAGS: machine-learning, python<br/>
      <p>&lt;div&gt;&lt;p&gt;{desc}&lt;/p&gt;&lt;/div&gt;</p>
    </div></content>
    <link type="image/jpeg" href="/opds/cover/{id}" rel="http://opds-spec.org/image"/>
    {links}
  </entry>"""

_ACQ = (
    '<link rel="http://opds-spec.org/acquisition" href="/opds/download/{id}/{fmt}/" '
    'length="{size}" title="{FMT}" mtime="x" type="application/{fmt}"/>'
)


def _entry(n: int, book_id: int, title: str, fmts=("epub", "pdf"), size: int = 1000) -> str:
    links = "\n".join(_ACQ.format(id=book_id, fmt=f, FMT=f.upper(), size=size) for f in fmts)
    return _ENTRY.format(
        n=n,
        id=book_id,
        title=title,
        uuid=f"uuid-{book_id}",
        author="Aurélien Géron",
        desc=f"A book about {title}.",
        links=links,
    )


def _feed(entries: str, next_offset: int | None = None) -> str:
    nxt = (
        f'<link rel="next" title="Next" href="/opds/new?offset={next_offset}" '
        'type="application/atom+xml;profile=opds-catalog;type=feed;kind=navigation"/>'
        if next_offset is not None
        else ""
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/terms/" xmlns:dcterms="http://purl.org/dc/terms/">
  <id>urn:uuid:2853dacf-ed79-42f5-8e8a-a7bb3d1ae6a2</id>
  <title>Calibre-Web</title>
  {nxt}
  {entries}
  <entry>
    <title>A shelf</title>
    <id>/opds/shelf/1</id>
    <link rel="subsection" type="application/atom+xml;profile=opds-catalog" href="/opds/shelf/1"/>
  </entry>
</feed>"""


PAGE_1 = _feed(_entry(1, 12, "Hands-On Machine Learning") + _entry(2, 7, "Deep Learning", ("pdf",)), 2)
PAGE_2 = _feed(_entry(3, 3, "Fluent Python", ("mobi",)))


def _conn(**kw) -> CalibreConnector:
    return CalibreConnector(BASE, kw.pop("user", "aegis"), kw.pop("password", "secret"), **kw)


def test_parse_feed_reads_the_calibre_web_entry_shape():
    books, nxt = parse_feed(PAGE_1, BASE)
    assert nxt == "/opds/new?offset=2"
    assert [b["id"] for b in books] == [12, 7], "the shelf navigation entry is not a book"
    b = books[0]
    assert b["title"] == "Hands-On Machine Learning"
    assert b["uuid"] == "uuid-12"
    assert b["authors"] == ["Aurélien Géron"]
    assert b["publisher"] == "O'Reilly"
    assert b["published"] == "2019-05-01"
    assert b["languages"] == ["eng"]
    assert b["tags"] == ["machine-learning", "python"]
    assert b["description"] == "A book about Hands-On Machine Learning."
    assert [f["format"] for f in b["formats"]] == ["EPUB", "PDF"]
    assert b["formats"][0]["href"] == f"{BASE}/opds/download/12/epub/"
    assert b["formats"][0]["size"] == 1000


def test_a_login_page_is_not_a_feed():
    with pytest.raises(CalibreError):
        parse_feed("<html><body>Sign in</body></html>")
    with pytest.raises(CalibreError):
        parse_feed("not xml at all")


def test_the_public_host_is_refused():
    with pytest.raises(ValueError, match="Cloudflare Access"):
        CalibreConnector("https://calibre.hikmahtech.in", "u", "p")


@pytest.mark.asyncio
@respx.mock
async def test_catalog_follows_next_links_and_is_cached():
    def page(request: httpx.Request) -> httpx.Response:
        body = PAGE_2 if request.url.params.get("offset") == "2" else PAGE_1
        return httpx.Response(200, text=body, headers={"content-type": "application/atom+xml"})

    route = respx.get(url__startswith=f"{BASE}/opds/new").mock(side_effect=page)
    conn = _conn()
    books = await conn.catalog()
    assert [b["id"] for b in books] == [12, 7, 3]
    assert route.call_count == 2
    assert route.calls[0].request.headers["authorization"].startswith("Basic ")
    again = await conn.catalog()
    assert [b["id"] for b in again] == [12, 7, 3]
    assert route.call_count == 2, "the second call is served from the cache"
    assert (await conn.get_book(7))["title"] == "Deep Learning"
    assert await conn.get_book(999) is None
    await conn.catalog(use_cache=False)
    assert route.call_count == 4
    await conn.close()


@pytest.mark.asyncio
@respx.mock
async def test_a_redirect_is_an_error_never_followed():
    respx.get(url__startswith=f"{BASE}/opds/new").mock(
        return_value=httpx.Response(302, headers={"location": "https://login.example/"})
    )
    conn = _conn()
    with pytest.raises(CalibreError, match="login page"):
        await conn.catalog()
    await conn.close()


@pytest.mark.asyncio
@respx.mock
async def test_bad_credentials_say_so():
    respx.get(url__startswith=f"{BASE}/opds/new").mock(return_value=httpx.Response(401))
    conn = _conn()
    with pytest.raises(CalibreError, match="401"):
        await conn.catalog()
    await conn.close()


@pytest.mark.asyncio
async def test_not_configured_makes_no_request():
    conn = _conn(user="", password="")
    assert not conn.configured
    with pytest.raises(CalibreError, match="not configured"):
        await conn.catalog()


@pytest.mark.asyncio
@respx.mock
async def test_search_passes_the_query():
    route = respx.get(f"{BASE}/opds/search").mock(return_value=httpx.Response(200, text=PAGE_2))
    conn = _conn()
    books = await conn.search("fluent python")
    assert [b["id"] for b in books] == [3]
    assert route.calls[0].request.url.params["query"] == "fluent python"
    await conn.close()


@pytest.mark.asyncio
@respx.mock
async def test_download_returns_the_file_and_respects_the_cap(monkeypatch):
    books, _ = parse_feed(PAGE_1, BASE)
    respx.get(f"{BASE}/opds/download/12/epub/").mock(
        return_value=httpx.Response(200, content=b"PK\x03\x04epub-bytes")
    )
    conn = _conn()
    assert await conn.download(books[0], "epub") == b"PK\x03\x04epub-bytes"

    with pytest.raises(CalibreError, match="no MOBI"):
        await conn.download(books[0], "mobi")

    import aegis.connectors.calibre as mod

    monkeypatch.setattr(mod, "MAX_DOWNLOAD_BYTES", 10)
    with pytest.raises(CalibreError, match="read limit"):
        await conn.download(books[0], "epub")  # the length attribute (1000) is already over
    await conn.close()
