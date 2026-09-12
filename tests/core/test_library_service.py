"""services/library.py — the Calibre library's shared logic (#510)."""

from __future__ import annotations

import io
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aegis.connectors.calibre import CalibreError
from aegis.services import library
from aegis.services import research as rs

# --------------------------------------------------------------------------
# EPUB fixtures
# --------------------------------------------------------------------------

_CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""

_CHAPTER = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>junk title</title>
<style>p {{ color: red }}</style></head>
<body><h1>{heading}</h1>{paras}<script>var x = 1;</script></body></html>"""


def _chapter(heading: str, paras: list[str]) -> str:
    return _CHAPTER.format(heading=heading, paras="".join(f"<p>{p}</p>" for p in paras))


INTRO = [
    "This book is a practical introduction to machine learning with Python.",
    "It assumes you can read code and want working systems.",
] * 5
GD = [
    "Gradient descent adjusts parameters step by step to reduce the loss.",
    "The learning rate sets the step size: too large and training diverges.",
    "Stochastic gradient descent uses one example at a time.",
] * 5


def make_epub(*, nav: bool = True) -> bytes:
    manifest = [
        '<item id="ch1" href="text/ch1.xhtml" media-type="application/xhtml+xml"/>',
        '<item id="ch2" href="text/ch2.xhtml" media-type="application/xhtml+xml"/>',
        '<item id="css" href="style.css" media-type="text/css"/>',
    ]
    if nav:
        manifest.append(
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        )
        spine_attr = ""
    else:
        manifest.append('<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>')
        spine_attr = ' toc="ncx"'
    opf = f"""<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <manifest>{''.join(manifest)}</manifest>
  <spine{spine_attr}><itemref idref="ch1"/><itemref idref="ch2"/></spine>
</package>"""
    nav_doc = """<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>
<nav epub:type="toc"><ol>
  <li><a href="text/ch1.xhtml">Introduction</a></li>
  <li><a href="text/ch2.xhtml#top">Gradient Descent</a></li>
</ol></nav></body></html>"""
    ncx = """<?xml version="1.0"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap>
  <navPoint id="p1"><navLabel><text>Intro (NCX)</text></navLabel><content src="text/ch1.xhtml"/></navPoint>
  <navPoint id="p2"><navLabel><text>Descent (NCX)</text></navLabel><content src="text/ch2.xhtml"/></navPoint>
</navMap></ncx>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", _CONTAINER)
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/text/ch1.xhtml", _chapter("Chapter One", INTRO))
        z.writestr("OEBPS/text/ch2.xhtml", _chapter("Chapter Two", GD))
        z.writestr("OEBPS/style.css", "p {}")
        if nav:
            z.writestr("OEBPS/nav.xhtml", nav_doc)
        else:
            z.writestr("OEBPS/toc.ncx", ncx)
    return buf.getvalue()


def test_epub_sections_follow_the_spine_and_the_nav_titles():
    sections = library.epub_sections(make_epub())
    assert [(s["n"], s["title"]) for s in sections] == [
        (1, "Introduction"),
        (2, "Gradient Descent"),
    ]
    assert "practical introduction" in sections[0]["text"]
    assert "var x" not in sections[0]["text"], "scripts are not text"
    assert "junk title" not in sections[0]["text"], "the <head> is not text"


def test_epub2_falls_back_to_the_ncx():
    sections = library.epub_sections(make_epub(nav=False))
    assert [s["title"] for s in sections] == ["Intro (NCX)", "Descent (NCX)"]


def test_a_broken_epub_is_a_calibre_error():
    with pytest.raises(CalibreError):
        library.epub_sections(b"not a zip")


# --------------------------------------------------------------------------
# Passages and pages
# --------------------------------------------------------------------------


def test_distinct_terms_beat_a_repeated_one():
    parts = [
        ("a", "loss loss loss loss loss loss"),
        ("b", "the learning rate controls gradient descent on the loss"),
    ]
    found = library.best_passages(parts, "how does the learning rate affect gradient descent")
    assert found[0]["where"] == "b"


def test_a_query_of_stopwords_finds_nothing():
    assert library.best_passages([("a", "the and for")], "the and for") == []


def test_parse_pages_clamps_to_the_book_and_the_span():
    assert library.parse_pages("12-18", 100) == (12, 18)
    assert library.parse_pages("5", 100) == (5, 5)
    assert library.parse_pages("18-12", 100) == (12, 18)
    assert library.parse_pages("90-200", 100) == (90, 100)
    assert library.parse_pages("1-500", 1000) == (1, library.PDF_MAX_SPAN)
    with pytest.raises(ValueError):
        library.parse_pages("chapter two", 100)


# --------------------------------------------------------------------------
# The index row
# --------------------------------------------------------------------------

BOOK = {
    "id": 12,
    "uuid": "u-12",
    "title": "Hands-On Machine Learning",
    "authors": ["Aurélien Géron"],
    "publisher": "O'Reilly",
    "published": "2019-05-01",
    "updated": "2026-01-01T10:00:00+00:00",
    "languages": ["eng"],
    "tags": ["machine-learning", "python"],
    "description": "A practical book.",
    "formats": [
        {"format": "EPUB", "href": "http://c/opds/download/12/epub/", "size": 10},
        {"format": "PDF", "href": "http://c/opds/download/12/pdf/", "size": 10},
    ],
}


def test_the_index_row_is_metadata_only():
    doc = library.book_document(BOOK)
    assert doc["url"] == "calibre://book/12"
    assert "Aurélien Géron" in doc["raw_text"] and "machine-learning" in doc["raw_text"]
    assert doc["metadata"]["calibre_id"] == 12
    assert doc["metadata"]["formats"] == ["EPUB", "PDF"]
    assert doc["tags"][0] == "book"


def test_the_fingerprint_moves_only_when_the_book_does():
    fp = library.book_fingerprint(BOOK)
    assert library.book_fingerprint(dict(BOOK)) == fp
    assert library.book_fingerprint({**BOOK, "description": "Revised."}) != fp
    assert library.book_fingerprint({**BOOK, "cover": "elsewhere"}) == fp


# --------------------------------------------------------------------------
# read_book against a fake connector
# --------------------------------------------------------------------------


class FakeConn:
    def __init__(self, book: dict, data: bytes):
        self.book = book
        self.data = data
        self.downloads: list[str] = []

    async def get_book(self, book_id):
        return self.book if int(book_id) == self.book["id"] else None

    async def download(self, book, fmt):
        self.downloads.append(fmt)
        return self.data


@pytest.mark.asyncio
async def test_read_an_epub_by_query_section_and_by_default():
    conn = FakeConn(BOOK, make_epub())
    by_query = await library.read_book(conn, 12, query="what does the learning rate do")
    assert conn.downloads == ["EPUB"], "EPUB is preferred over PDF"
    assert by_query["passages"][0]["cite"].startswith("Hands-On Machine Learning, chapter 2")
    assert "learning rate" in by_query["passages"][0]["text"]

    by_section = await library.read_book(conn, 12, section="gradient")
    assert by_section["section"] == {"n": 2, "title": "Gradient Descent"}
    assert by_section["cite"] == "Hands-On Machine Learning, chapter 2 (Gradient Descent)"

    by_number = await library.read_book(conn, 12, section="1")
    assert by_number["section"]["n"] == 1

    missing = await library.read_book(conn, 12, section="appendix z")
    assert "no chapter" in missing["error"] and missing["toc"]

    short = await library.read_book(conn, 12, section="2", max_chars=600)
    assert short["truncated"] is True and len(short["text"]) <= 600


@pytest.mark.asyncio
async def test_read_a_pdf_by_pages_and_by_query(monkeypatch):
    pdf_book = {**BOOK, "formats": [BOOK["formats"][1]]}
    conn = FakeConn(pdf_book, b"%PDF-1.4")
    monkeypatch.setattr(library, "pdf_page_count", lambda data: 40)
    calls: list[tuple[int, int]] = []

    def pages(data, first, last):
        calls.append((first, last))
        return [f"page {n} text about optimisers" if n == 7 else f"page {n}" for n in range(first, last + 1)]

    monkeypatch.setattr(library, "pdf_pages", pages)
    ranged = await library.read_book(conn, 12, pages="3-5")
    assert ranged["cite"] == "Hands-On Machine Learning, pp. 3-5"
    assert "[p. 3]" in ranged["text"] and calls[-1] == (3, 5)

    default = await library.read_book(conn, 12)
    assert default["pages"] == {"first": 1, "last": library.PDF_DEFAULT_PAGES}

    found = await library.read_book(conn, 12, query="optimisers", pdf_scan_pages=10)
    assert calls[-1] == (1, 10)
    assert found["passages"][0]["cite"] == "Hands-On Machine Learning, p. 7"

    bad = await library.read_book(conn, 12, pages="the end")
    assert "pages must be" in bad["error"]


@pytest.mark.asyncio
async def test_an_unreadable_format_is_an_answer_not_a_crash():
    mobi = {**BOOK, "formats": [{"format": "MOBI", "href": "x", "size": 1}]}
    result = await library.read_book(FakeConn(mobi, b""), 12)
    assert "only EPUB and PDF" in result["error"]
    assert (await library.read_book(FakeConn(BOOK, b""), 999))["error"].startswith("there is no book")


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def test_connector_or_reason():
    library._connectors.clear()
    assert library.connector_or_reason(SimpleNamespace())[1] == "not_configured"
    public = SimpleNamespace(
        calibre_url="https://calibre.hikmahtech.in", calibre_user="u", calibre_password="p"
    )
    conn, reason = library.connector_or_reason(public)
    assert conn is None and reason.startswith("refused")

    s = SimpleNamespace(calibre_url="", calibre_user="u", calibre_password="p")
    first, _ = library.connector_or_reason(s)
    assert first is not None and first.base_url == "http://calibre-web_calibre-web:8083"
    assert library.connector_or_reason(s)[0] is first, "the same config reuses the connector"
    s.calibre_password = "changed"
    assert library.connector_or_reason(s)[0] is not first, "a new password builds a new one"
    library._connectors.clear()


# --------------------------------------------------------------------------
# Suggest, and books in research sources
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suggest_uses_the_index_then_calibre_search():
    kc = AsyncMock()
    kc.search = AsyncMock(
        return_value=[
            {
                "title": "Hands-On Machine Learning",
                "summary": "A practical book.",
                "similarity": 0.71234,
                "metadata": {"calibre_id": 12, "authors": ["A"], "tags": ["ml"], "formats": ["PDF"]},
            }
        ]
    )
    via_index = await library.suggest_books(kc, None, "neural networks")
    assert via_index["via"] == "index"
    assert via_index["books"][0] == {
        "id": 12,
        "title": "Hands-On Machine Learning",
        "authors": ["A"],
        "tags": ["ml"],
        "formats": ["PDF"],
        "similarity": 0.712,
        "summary": "A practical book.",
    }
    assert kc.search.call_args.kwargs["source_type"] == "book"

    kc.search = AsyncMock(return_value=[])
    conn = AsyncMock()
    conn.search = AsyncMock(return_value=[BOOK])
    via_search = await library.suggest_books(kc, conn, "neural networks")
    assert via_search["via"] == "calibre_search" and via_search["books"][0]["id"] == 12


def test_books_are_cited_but_their_keys_are_not_shown():
    sources = rs.build_sources(
        [],
        [],
        [],
        [],
        books=[
            {
                "title": "Hands-On ML",
                "cite": "Hands-On ML, chapter 2 (Gradient Descent)",
                "url": "calibre://book/12",
                "passage": "Gradient descent adjusts parameters.",
            }
        ],
    )
    assert sources[0]["kind"] == "book"
    assert sources[0]["title"] == "Hands-On ML, chapter 2 (Gradient Descent)"
    prompt = rs.synthesis_prompt("q", "", sources)
    assert "calibre://" not in prompt and "Gradient descent adjusts" in prompt
    report = rs.render_report("Answer [1].", rs.public_sources(sources))
    assert "calibre://" not in report
    assert "[1] Hands-On ML, chapter 2 (Gradient Descent)" in report
